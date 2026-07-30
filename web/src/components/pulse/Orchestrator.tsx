import {
  Check,
  ChevronDown,
  Cpu,
  MonitorPlay,
  RefreshCw,
  TriangleAlert,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { HudFrame } from "../hud/HudFrame";
import { api, ApiError } from "../../lib/api";
import { engineBadge, relTime } from "../../lib/format";
import type {
  EvidenceKind,
  OrchestratorAction,
  OrchestratorConfig,
  OrchestratorTier,
} from "../../types/api";
import styles from "./Orchestrator.module.css";

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
const DELIVERING_FALLBACK = new Set(["continue", "choose", "answer"]);

/** Jump target for an action: the session view at /s/:engine/:uuid. */
function sessionPath(a: OrchestratorAction): string {
  const uuid = a.session_id.slice(a.session_id.indexOf(":") + 1);
  return `/s/${encodeURIComponent(a.engine)}/${encodeURIComponent(uuid)}`;
}

/** Status colour is load-bearing (docs/design.md): amber `degraded` means "needs a decision",
 *  red `down` is reserved for genuine failure. An escalation is NOT an incident. */
function toneOf(a: OrchestratorAction): string {
  if (a.state === "failed" || a.state === "indeterminate")
    return styles.toneDown;
  if (a.state === "escalated" || a.state === "proposed")
    return styles.toneDegraded;
  if (a.state === "delivered" || a.state === "approved") return styles.toneUp;
  return styles.toneIdle;
}

/** Server-pulled evidence, fetched on expand and never cached — the operator must always read
 *  the CURRENT screen, not the one the pass happened to see. The model never supplies this
 *  text; it only names a `kind`, because a model that can quote a screen can invent one. */
function EvidenceBlock({
  sessionId,
  kind,
}: {
  sessionId: string;
  kind: EvidenceKind;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(false);
  // Monotonic id of the CURRENT open. Every fetch is stamped with the open it belongs to, and
  // a reply from a superseded open is discarded. Without this, close-then-reopen while the
  // first request is still in flight lets that first (now stale) snapshot populate the
  // reopened panel — the operator then judges a screen the session has already left.
  const openSeq = useRef(0);

  const load = useCallback(
    async (seq: number) => {
      setLoading(true);
      setFailed(false);
      try {
        const e = await api.evidence(sessionId, kind);
        if (openSeq.current !== seq) return; // superseded by a newer open
        setText(e.available ? e.text : "");
      } catch {
        if (openSeq.current === seq) setFailed(true);
      } finally {
        if (openSeq.current === seq) setLoading(false);
      }
    },
    [sessionId, kind],
  );

  const toggle = useCallback(() => {
    const next = !open;
    setOpen(next);
    if (next) {
      // Re-fetch on EVERY open, and unconditionally — gating on `loading` would skip the
      // request for this open whenever a previous one were still pending, which is exactly
      // the race that leaves a stale screen on screen.
      const seq = openSeq.current + 1;
      openSeq.current = seq;
      setText(null);
      void load(seq);
    } else {
      // Closing invalidates any in-flight reply, so it can't land in a later open.
      openSeq.current += 1;
    }
  }, [open, load]);

  if (kind === "none") return null;
  const label =
    kind === "screen"
      ? "LIVE SCREEN"
      : kind === "recap"
        ? "RECAP"
        : "TRANSCRIPT";
  return (
    <div className={styles.evd}>
      <button
        type="button"
        className={styles.evdHead}
        onClick={toggle}
        aria-expanded={open}
        aria-label={`${open ? "Hide" : "Show"} ${label.toLowerCase()} for this session`}
      >
        <MonitorPlay size={11} aria-hidden="true" />
        {label}
        <ChevronDown
          size={11}
          className={open ? styles.chevOpen : undefined}
          aria-hidden="true"
        />
      </button>
      {open && (
        <div className={styles.evdBody}>
          {loading && (
            <span className={styles.muted}>Reading the session…</span>
          )}
          {!loading && failed && (
            <span className={styles.muted}>
              Couldn’t read this session right now.
            </span>
          )}
          {!loading && !failed && text === "" && (
            <span className={styles.muted}>
              Nothing on screen — the session has no output yet.
            </span>
          )}
          {!loading && !failed && text ? (
            <pre className={styles.pre}>{text}</pre>
          ) : null}
        </div>
      )}
    </div>
  );
}

/** One proposed/recorded action. All model-derived text (`rationale`, `answer`) renders as
 *  plain text via React's default escaping — never markup. */
function ActionRow({
  action,
  onResolved,
  onNote,
  deliveringVerbs,
}: {
  action: OrchestratorAction;
  onResolved?: (a: OrchestratorAction) => void;
  onNote?: (msg: string) => void;
  deliveringVerbs?: Set<string>;
}) {
  const delivering = (deliveringVerbs ?? DELIVERING_FALLBACK).has(action.verb);
  const [busy, setBusy] = useState<"" | "approve" | "reject">("");
  const [note, setNote] = useState<string | null>(null);
  // Approvable only while it is still awaiting a decision AND the verb actually delivers.
  const waiting = action.state === "proposed" || action.state === "approved";
  // Approve requires something to DELIVER; dismissing does not. An `escalated` row has no
  // delivering verb by definition — it is the orchestrator saying "you look at this" — so
  // gating Reject on deliverability left it stuck under "Needs a decision", blocking another
  // proposal for that session until it expired. The backend has always allowed it:
  // REJECTABLE_STATES includes `escalated`.
  const approvable = delivering && waiting;
  const rejectable = waiting || action.state === "escalated";

  const act = useCallback(
    async (which: "approve" | "reject") => {
      if (busy) return;
      setBusy(which);
      setNote(null);
      try {
        const r =
          which === "approve"
            ? await api.approveAction(action.id)
            : await api.rejectAction(action.id);
        onResolved?.(r);
      } catch (e) {
        // 409 is the compare-and-execute verdict, not a bug: the session moved on between the
        // proposal and the tap, so nothing was written. Say that plainly.
        if (e instanceof ApiError && e.status === 409) {
          // The server has ALREADY settled this record (stale/expired/claimed) — the 409 is
          // its verdict, not a transient error. Leaving the row actionable meant every retry
          // returned 409 again until a page refresh, so fold the settled record through the
          // normal resolve path when the server sent one.
          // Consuming the settled record REMOVES this row from pending — which is right, but
          // it takes the inline note with it. Raise the explanation to the panel so the
          // operator sees why an action they just clicked vanished.
          const msg = `Not sent — ${e.message}`;
          // Only fold the body in when it actually IS a settled record. A 409 may carry just
          // `{detail}` — treating that as an action pushed a malformed row into the feed and
          // left the real one in place, because the filter matched on an `id` that wasn't
          // there. Shape-check, then choose where the explanation goes: the panel when the row
          // is about to disappear, the row itself when it stays.
          const rec = e.record as Partial<OrchestratorAction> | undefined;
          if (rec?.id && rec?.state) {
            onNote?.(msg);
            onResolved?.(rec as OrchestratorAction);
          } else {
            setNote(msg);
          }
        } else {
          setNote("Couldn’t complete that — please try again.");
        }
      } finally {
        setBusy("");
      }
    },
    [action.id, busy, onResolved, onNote],
  );

  return (
    <li className={`${styles.act} ${toneOf(action)}`}>
      <div className={styles.actTop}>
        <span
          className={`${styles.verb} ${action.verb === "escalate" ? styles.verbEsc : ""}`}
        >
          {action.verb.toUpperCase()}
          {action.verb === "choose" && action.option !== undefined
            ? ` ${action.option}`
            : ""}
        </span>
        <span className={styles.sname}>
          {action.title || action.session_id}
        </span>
        <span className={styles.eng} aria-hidden="true">
          {engineBadge(action.engine)}
        </span>
        <span className={styles.conf}>
          conf {action.confidence.toFixed(2)}
          {action.state === "escalated" ? " · below threshold" : ""}
        </span>
      </div>
      {action.rationale && <p className={styles.why}>{action.rationale}</p>}
      {action.answer && <p className={styles.why}>{`“${action.answer}”`}</p>}
      {/* Evidence sits ABOVE the buttons on purpose: the operator should be able to see what
          the session is actually showing before they authorise typing into it. */}
      <EvidenceBlock sessionId={action.session_id} kind={action.evidence} />
      {note && <p className={styles.stale}>{note}</p>}
      {(approvable || rejectable) && (
        <div className={styles.btns}>
          {approvable && (
            <button
              type="button"
              className={styles.approve}
              disabled={!!busy}
              onClick={() => void act("approve")}
            >
              <Check size={13} aria-hidden="true" />
              {busy === "approve" ? "Sending…" : "Approve"}
            </button>
          )}
          {rejectable && (
            <button
              type="button"
              className={styles.reject}
              disabled={!!busy}
              // Keyed off STATE, not off whether Approve happens to be offered: a proposed
              // action with a non-deliverable verb is not an escalation, and calling it one
              // mislabels the control for a screen reader.
              aria-label={
                action.state === "escalated"
                  ? "Dismiss this escalation"
                  : "Reject this action"
              }
              onClick={() => void act("reject")}
            >
              <X size={13} aria-hidden="true" />
            </button>
          )}
        </div>
      )}
      <div className={styles.actFoot}>
        <span className={styles.state}>{action.state}</span>
        <span className={styles.age}>{relTime(action.ts)}</span>
        <Link className={styles.jump} to={sessionPath(action)}>
          Open session
        </Link>
      </div>
    </li>
  );
}

/** The activity feed, grouped by project — so "which of my projects needs me" is answerable
 *  at a glance rather than by scanning a flat list. */
function ByProject({
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
  const groups = useMemo(() => {
    const m = new Map<string, OrchestratorAction[]>();
    for (const a of actions) {
      const k = a.project || "Unfiled";
      const list = m.get(k);
      if (list) list.push(a);
      else m.set(k, [a]);
    }
    return [...m.entries()];
  }, [actions]);

  return (
    <>
      {groups.map(([project, rows]) => (
        <div key={project} className={styles.group}>
          <div className={styles.groupHead}>
            <b>{project}</b>
            <span>
              {rows.length} action{rows.length === 1 ? "" : "s"}
            </span>
          </div>
          <ul className={styles.list}>
            {rows.map((a) => (
              <ActionRow
                key={a.id}
                action={a}
                onResolved={onResolved}
                onNote={onNote}
                deliveringVerbs={deliveringVerbs}
              />
            ))}
          </ul>
        </div>
      ))}
    </>
  );
}

/** Pulse orchestrator surface (#726 Phase 1) — the AUTONOMY strip plus the proposal feed.
 *  Lives on the Pulse page under its existing PULSE header; the feature adds no new route and
 *  no new product name. Phase 1 proposes and never writes. */
/** Mirrors the server's FEED_LIMIT so the client cannot grow an unbounded list. */
const FEED_MAX = 100;

export function Orchestrator({ onTierChange }: { onTierChange?: () => void }) {
  const [config, setConfig] = useState<OrchestratorConfig | null>(null);
  const [pending, setPending] = useState<OrchestratorAction[]>([]);
  const [feed, setFeed] = useState<OrchestratorAction[]>([]);
  // Server-owned: which verbs the actuator can actually render. Undefined until the first
  // load (and on an older server), where the row falls back to the conservative local set.
  const [deliveringVerbs, setDeliveringVerbs] = useState<
    Set<string> | undefined
  >(undefined);
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
      })
      .catch(() => undefined)
      .finally(() => live && setLoaded(true));
    return () => {
      live = false;
    };
  }, []);

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
      if (s.assessment) setNote(s.assessment);
    } catch (e) {
      setNote(
        e instanceof ApiError
          ? e.message
          : "The pass failed — please try again.",
      );
    } finally {
      setRunning(false);
    }
  }, [running]);

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
      {note && <p className={styles.note}>{note}</p>}

      {pending.length > 0 && (
        <div className={styles.block}>
          <div className={styles.blockHead}>
            <TriangleAlert size={13} aria-hidden="true" />
            <h3>Needs a decision · {pending.length}</h3>
          </div>
          <ByProject
            actions={pending}
            onResolved={resolve}
            onNote={setNote}
            deliveringVerbs={deliveringVerbs}
          />
        </div>
      )}

      {feed.length > 0 ? (
        <div className={styles.block}>
          <div className={styles.blockHead}>
            <h3>Activity</h3>
            <span className={styles.sub}>grouped by project</span>
          </div>
          <ByProject
            actions={feed}
            onResolved={resolve}
            onNote={setNote}
            deliveringVerbs={deliveringVerbs}
          />
        </div>
      ) : (
        <p className={styles.empty}>
          {config.enabled
            ? "Nothing proposed yet. The orchestrator runs on its own schedule, or you can run a pass now."
            : "The orchestrator is off. Turn it on in Settings to have it watch your sessions."}
        </p>
      )}
    </section>
  );
}
