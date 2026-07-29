import { Check, ChevronDown, Cpu, MonitorPlay, RefreshCw, TriangleAlert } from "lucide-react";
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
  { id: "off", label: "OFF", hint: "Observe and propose only — nothing is ever sent." },
  { id: "suggest", label: "SUGGEST", hint: "Every action waits for your approval." },
  {
    id: "yolo",
    label: "YOLO",
    hint: "Acts on its own above the confidence threshold — nudges only.",
  },
];

/** Verbs that would put bytes on a session's stdin. `observe`/`escalate` are decisions. */
const DELIVERING = new Set(["continue", "choose", "answer", "dispatch"]);

/** Jump target for an action: the session view at /s/:engine/:uuid. */
function sessionPath(a: OrchestratorAction): string {
  const uuid = a.session_id.slice(a.session_id.indexOf(":") + 1);
  return `/s/${encodeURIComponent(a.engine)}/${encodeURIComponent(uuid)}`;
}

/** Status colour is load-bearing (docs/design.md): amber `degraded` means "needs a decision",
 *  red `down` is reserved for genuine failure. An escalation is NOT an incident. */
function toneOf(a: OrchestratorAction): string {
  if (a.state === "failed" || a.state === "indeterminate") return styles.toneDown;
  if (a.state === "escalated" || a.state === "proposed") return styles.toneDegraded;
  if (a.state === "delivered" || a.state === "approved") return styles.toneUp;
  return styles.toneIdle;
}

/** Server-pulled evidence, fetched on expand and never cached — the operator must always read
 *  the CURRENT screen, not the one the pass happened to see. The model never supplies this
 *  text; it only names a `kind`, because a model that can quote a screen can invent one. */
function EvidenceBlock({ sessionId, kind }: { sessionId: string; kind: EvidenceKind }) {
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
  const label = kind === "screen" ? "LIVE SCREEN" : kind === "recap" ? "RECAP" : "TRANSCRIPT";
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
          {loading && <span className={styles.muted}>Reading the session…</span>}
          {!loading && failed && (
            <span className={styles.muted}>Couldn’t read this session right now.</span>
          )}
          {!loading && !failed && text === "" && (
            <span className={styles.muted}>Nothing on screen — the session has no output yet.</span>
          )}
          {!loading && !failed && text ? <pre className={styles.pre}>{text}</pre> : null}
        </div>
      )}
    </div>
  );
}

/** One proposed/recorded action. All model-derived text (`rationale`, `answer`) renders as
 *  plain text via React's default escaping — never markup. */
function ActionRow({ action, phase1 }: { action: OrchestratorAction; phase1: boolean }) {
  const delivering = DELIVERING.has(action.verb);
  return (
    <li className={`${styles.act} ${toneOf(action)}`}>
      <div className={styles.actTop}>
        <span className={`${styles.verb} ${action.verb === "escalate" ? styles.verbEsc : ""}`}>
          {action.verb.toUpperCase()}
          {action.verb === "choose" && action.option !== undefined ? ` ${action.option}` : ""}
        </span>
        <span className={styles.sname}>{action.title || action.session_id}</span>
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
      <EvidenceBlock sessionId={action.session_id} kind={action.evidence} />
      <div className={styles.actFoot}>
        <span className={styles.state}>{action.state}</span>
        <span className={styles.age}>{relTime(action.ts)}</span>
        {/* Phase 1 has no PTY write path at all, so a deliverable verb is explicitly a
            proposal — saying "would send" is the honest label until Phase 2 lands. */}
        {phase1 && delivering && action.state === "proposed" && (
          <span className={styles.wouldSend}>would send · approval arrives with actuation</span>
        )}
        <Link className={styles.jump} to={sessionPath(action)}>
          Open session
        </Link>
      </div>
    </li>
  );
}

/** The activity feed, grouped by project — so "which of my projects needs me" is answerable
 *  at a glance rather than by scanning a flat list. */
function ByProject({ actions, phase1 }: { actions: OrchestratorAction[]; phase1: boolean }) {
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
              <ActionRow key={a.id} action={a} phase1={phase1} />
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
export function Orchestrator({ onTierChange }: { onTierChange?: () => void }) {
  const [config, setConfig] = useState<OrchestratorConfig | null>(null);
  const [pending, setPending] = useState<OrchestratorAction[]>([]);
  const [feed, setFeed] = useState<OrchestratorAction[]>([]);
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
        setNote(e instanceof ApiError ? e.message : "Couldn’t change the autonomy tier.");
      }
    },
    [config, onTierChange],
  );

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
      setNote(e instanceof ApiError ? e.message : "The pass failed — please try again.");
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
          <RefreshCw size={13} className={running ? styles.spin : undefined} aria-hidden="true" />
          {running ? "Thinking…" : "Run now"}
        </button>
      </div>

      {/* The tier alone never tells the whole story — say which verbs it can actually deliver. */}
      <p className={styles.ceiling}>
        <Check size={11} aria-hidden="true" /> Acts on its own: <b>{ceiling}</b> only. Everything
        else always waits for you.
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
          <ByProject actions={pending} phase1 />
        </div>
      )}

      {feed.length > 0 ? (
        <div className={styles.block}>
          <div className={styles.blockHead}>
            <h3>Activity</h3>
            <span className={styles.sub}>grouped by project</span>
          </div>
          <ByProject actions={feed} phase1 />
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
