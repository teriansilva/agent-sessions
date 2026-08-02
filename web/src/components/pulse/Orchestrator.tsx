import { AlertTriangle, Check, Cpu, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { HudFrame } from "../hud/HudFrame";
import { api, ApiError } from "../../lib/api";
import type {
  AiTaskLast,
  OrchestratorAction,
  OrchestratorConfig,
  OrchestratorTier,
} from "../../types/api";
import styles from "./Orchestrator.module.css";
import { ActionRow } from "./ActionRow";
import { relTime } from "../../lib/format";

const TIERS: { id: OrchestratorTier; label: string; hint: string }[] = [
  {
    id: "off",
    label: "OFF",
    hint: "Observe and propose only — nothing is ever sent.",
  },
  {
    id: "suggest",
    label: "SUGGEST",
    hint: "Every action waits for your approval.",
  },
  {
    id: "yolo",
    label: "YOLO",
    hint: "Acts on its own above the confidence threshold — nudges only.",
  },
];

/** Verbs that would put bytes on a session's stdin. `observe`/`escalate` are decisions. */
// Fallback only, for a server that predates `delivering_verbs`. It deliberately does NOT
// include `dispatch`: the actuator cannot render it, so offering Approve guaranteed a 409.
/** Jump target for an action: the session view at /s/:engine/:uuid. */
/** The activity feed as ONE grid (#754).
 *
 * It used to group by project, one `<ul>` per group. In a grid that costs a partial row per
 * project — measured at 1900px, a two-action project filled 2 of 4 tracks and left the rest
 * empty, which is the same wasted width the section headings cost the session cards above.
 * The project moves onto the row instead, so every column is used and the feed still answers
 * "which project" per item. Order is the server's: newest first.
 */
function Feed({
  actions,
  onResolved,
  onNote,
  deliveringVerbs,
}: {
  actions: OrchestratorAction[];
  onResolved?: (a: OrchestratorAction) => void;
  onNote?: (msg: string) => void;
  deliveringVerbs?: Set<string>;
}) {
  return (
    <ul className={styles.list}>
      {actions.map((a) => (
        <ActionRow
          key={a.id}
          action={a}
          onResolved={onResolved}
          onNote={onNote}
          deliveringVerbs={deliveringVerbs}
        />
      ))}
    </ul>
  );
}

/** Pulse orchestrator surface (#726 Phase 1) — the AUTONOMY strip plus the proposal feed.
 *  Lives on the Pulse page under its existing PULSE header; the feature adds no new route and
 *  no new product name. Phase 1 proposes and never writes. */
/** Mirrors the server's FEED_LIMIT so the client cannot grow an unbounded list. */
const FEED_MAX = 100;

export function Orchestrator({
  onTierChange,
  onActionsChanged,
  refreshKey = 0,
}: {
  onTierChange?: () => void;
  /** Bumped by the page when an action is resolved from a session CARD. The panel owns its own
   *  `pending`/`feed`, so without this its summary could read "1 action needs you" above a card
   *  that no longer has one, and the settled record would not reach Activity until a reload. */
  refreshKey?: number;
  /** A pass may create or settle actions that render on the session cards, which this
   *  component does not own — the page reloads its overview when told. */
  onActionsChanged?: () => void;
}) {
  const [config, setConfig] = useState<OrchestratorConfig | null>(null);
  const [pending, setPending] = useState<OrchestratorAction[]>([]);
  const [feed, setFeed] = useState<OrchestratorAction[]>([]);
  // Server-owned: which verbs the actuator can actually render. Undefined until the first
  // load (and on an older server), where the row falls back to the conservative local set.
  const [deliveringVerbs, setDeliveringVerbs] = useState<
    Set<string> | undefined
  >(undefined);
  // Last run of the scheduled pass. A run of failures here is the difference between
  // "nothing needs you" and "nothing has been LOOKED AT since yesterday evening" (#772).
  const [health, setHealth] = useState<AiTaskLast | undefined>(undefined);
  const [running, setRunning] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let live = true;
    // Defensive: the orchestrator strip is one panel on a page that must render without it.
    // A throw here — endpoint down, route missing on an older server — degrades to "no strip",
    // never a blank Pulse.
    Promise.resolve()
      .then(() => api.orchestrator())
      .then((s) => {
        if (!live) return;
        setConfig(s.config);
        setPending(s.pending);
        setFeed(s.feed);
        setDeliveringVerbs(
          s.delivering_verbs ? new Set(s.delivering_verbs) : undefined,
        );
        setHealth(s.last?.orchestrator);
      })
      .catch(() => undefined)
      .finally(() => live && setLoaded(true));
    return () => {
      live = false;
    };
    // Re-runs when the page bumps `refreshKey` — i.e. when an action was resolved from a card,
    // which this component cannot observe on its own.
  }, [refreshKey]);

  const setTier = useCallback(
    async (tier: OrchestratorTier) => {
      if (!config || tier === config.autonomy) return;
      setConfig({ ...config, autonomy: tier });
      try {
        await api.setPrefs({ orchestrator: { autonomy: tier } });
        onTierChange?.();
      } catch (e) {
        setConfig(config); // roll back — the server rejected it
        setNote(
          e instanceof ApiError
            ? e.message
            : "Couldn’t change the autonomy tier.",
        );
      }
    },
    [config, onTierChange],
  );

  // Fold a settled action back into both lists in place, so approving one row doesn't
  // re-order or reload the rest under the operator's cursor.
  const resolve = useCallback((r: OrchestratorAction) => {
    const settled = r.state !== "proposed" && r.state !== "approved";
    setPending((prev) =>
      settled
        ? prev.filter((a) => a.id !== r.id)
        : prev.map((a) => (a.id === r.id ? r : a)),
    );
    // UPSERT, not map. The server now returns `pending` and `feed` as disjoint sets, so a row
    // being settled here is BY CONTRACT absent from feed — `map` had nothing to update, and the
    // action vanished from "Needs a decision" without ever appearing in Activity. It only came
    // back on a refresh, which reads as the action having been lost.
    setFeed((prev) => {
      if (prev.some((a) => a.id === r.id)) {
        return prev.map((a) => (a.id === r.id ? r : a));
      }
      // Newest first, matching the server's ordering, and bounded the same way.
      return settled ? [r, ...prev].slice(0, FEED_MAX) : prev;
    });
  }, []);

  const runNow = useCallback(async () => {
    if (running) return;
    setRunning(true);
    setNote(null);
    try {
      const s = await api.orchestrate();
      setPending(s.pending);
      setFeed(s.feed);
      // The pass that just ran IS the newest health evidence. Without this the degraded line
      // survives the very "Run now" that fixed the endpoint — and "Run now" is what the
      // operator is told to click to force recovery, so that is the one case it must get
      // right (#772 review).
      setHealth(s.last?.orchestrator);
      if (s.assessment) setNote(s.assessment);
      // A pass can propose NEW actions, and those live on the session cards now — this panel
      // says so directly above. Without telling the page, the claim is false until a reload:
      // the panel would show "N actions need you — shown on the session cards below" while no
      // card had one (#754).
      onActionsChanged?.();
    } catch (e) {
      setNote(
        e instanceof ApiError
          ? e.message
          : "The pass failed — please try again.",
      );
    } finally {
      setRunning(false);
    }
  }, [running, onActionsChanged]);

  // Two failures, or a first-ever run that failed. `consecutive_failures` is server-owned so
  // the client is not inventing its own idea of what counts as an outage.
  const fails = health?.consecutive_failures ?? 0;
  const degraded = !!health && !health.ok && fails >= 2;
  const lastOkAgo = health?.last_ok ? relTime(health.last_ok) : "";

  if (!loaded || !config) return null;

  const tierHint = TIERS.find((t) => t.id === config.autonomy)?.hint ?? "";
  // The ceiling is server-owned: showing it stops the tier from implying more than it grants.
  const ceiling = config.auto_verbs_ceiling.join(", ");

  return (
    <section className={styles.wrap} aria-label="Orchestrator">
      <HudFrame />
      <div className={styles.head}>
        <Cpu size={14} aria-hidden="true" />
        <h2 className={styles.h2}>Autonomy</h2>
        <span className={styles.sub}>{tierHint}</span>
        <div className={styles.seg} role="group" aria-label="Autonomy tier">
          {TIERS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`${styles.segBtn} ${config.autonomy === t.id ? styles.segOn : ""} ${
                t.id === "yolo" ? styles.segYolo : ""
              }`}
              aria-pressed={config.autonomy === t.id}
              title={t.hint}
              onClick={() => void setTier(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      <div className={styles.meterRow}>
        <span className={styles.meterLabel}>ACT THRESHOLD</span>
        <span
          className={styles.bar}
          role="img"
          aria-label={`Confidence threshold ${config.confidence_min}`}
        >
          <i style={{ width: `${config.confidence_min * 100}%` }} />
        </span>
        <span className={styles.meterText}>
          conf ≥ {config.confidence_min.toFixed(2)} · below → escalate
        </span>
        <button
          type="button"
          className={styles.runBtn}
          onClick={() => void runNow()}
          disabled={running || !config.configured}
        >
          <RefreshCw
            size={13}
            className={running ? styles.spin : undefined}
            aria-hidden="true"
          />
          {running ? "Thinking…" : "Run now"}
        </button>
      </div>

      {/* The tier alone never tells the whole story — say which verbs it can actually deliver. */}
      <p className={styles.ceiling}>
        <Check size={11} aria-hidden="true" /> Acts on its own: <b>{ceiling}</b>{" "}
        only. Everything else always waits for you.
      </p>

      {!config.configured && (
        <p className={styles.hint}>
          Needs the AI endpoint — configure it in{" "}
          <Link to="/settings/ai-review">Settings → AI Review</Link>.
        </p>
      )}
      {/* Configured but FAILING is a third state, and it used to render exactly like a quiet
          day: the endpoint was down for 11 hours, every pass threw, and the page said nothing
          (#772). Only past a run of failures — one is a blip and must stay silent. The
          endpoint's own message is a remote response body, so it renders as plain text. */}
      {degraded && (
        <p className={styles.degraded} role="status">
          <AlertTriangle size={13} aria-hidden="true" />
          <span>
            The orchestrator can’t reach its AI endpoint
            {lastOkAgo
              ? ` — last successful pass ${lastOkAgo}`
              : " — no pass has succeeded yet"}
            .{health?.error ? ` ${health.error}` : ""}{" "}
            <Link to="/settings/ai-review">Check Settings → AI Review</Link>.
          </span>
        </p>
      )}
      {note && <p className={styles.note}>{note}</p>}

      {/* The pending queue used to render here as a second list beside the session cards.
          Measured against the live stores it was a strict SUBSET of them — every action's
          session already appeared under "Needs you", and nothing was exclusive to the queue —
          so one session rendered twice, in two visual languages, with two different
          affordances (#754). The decision controls now live on the card itself; this panel
          keeps what is genuinely its own: the autonomy tier, the threshold, and the feed. */}
      {pending.length > 0 && (
        <p className={styles.hint}>
          {pending.length}{" "}
          {pending.length === 1 ? "action needs" : "actions need"} you — shown
          on the session cards below.
        </p>
      )}

      {feed.length > 0 ? (
        <div className={styles.block}>
          <div className={styles.blockHead}>
            <h3>Activity</h3>
            <span className={styles.sub}>newest first</span>
          </div>
          <Feed
            actions={feed}
            onResolved={resolve}
            onNote={setNote}
            deliveringVerbs={deliveringVerbs}
          />
        </div>
      ) : pending.length > 0 ? null : ( // NOT "nothing proposed" while actions are waiting on
        // the cards — that read as a contradiction with the line directly above it (#754).
        <p className={styles.empty}>
          {config.enabled
            ? "Nothing proposed yet. The orchestrator runs on its own schedule, or you can run a pass now."
            : "The orchestrator is off. Turn it on in Settings to have it watch your sessions."}
        </p>
      )}
    </section>
  );
}
