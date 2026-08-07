import { Check, ChevronDown, MonitorPlay, X } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../../lib/api";
import { announceActionResolved } from "../../lib/actionEvents";
import { engineBadge, relTime } from "../../lib/format";
import type { EvidenceKind, OrchestratorAction } from "../../types/api";
import styles from "./Orchestrator.module.css";
import {
  DELIVERING_FALLBACK,
  escalationSuffix,
  sessionPath,
} from "../../lib/orchestratorAction";

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
  trailing,
}: {
  sessionId: string;
  kind: EvidenceKind;
  /** Rendered beside the disclosure's HEAD, not below it (#781) — the decision controls share
   *  that row so they cost no extra line. Kept out of the body's way on purpose: an expanded
   *  recap needs the full column width. */
  trailing?: React.ReactNode;
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
      <div className={styles.evdHeadRow}>
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
        {trailing}
      </div>
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
export function ActionRow({
  action,
  onResolved,
  onNote,
  deliveringVerbs,
  embedded,
}: {
  action: OrchestratorAction;
  onResolved?: (a: OrchestratorAction) => void;
  onNote?: (msg: string) => void;
  deliveringVerbs?: Set<string>;
  /** Rendered inside a session card, which already names the session. */
  embedded?: boolean;
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
  const escSuffix = escalationSuffix(action);

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
            // A 409 IS a resolution: the server has already settled this action terminally and
            // retired its bell row. The api client only announces on the fulfilled path, so
            // without this the badge keeps counting an alert the server has already dropped
            // until the next 60s poll. Announced here rather than in `api` because this is
            // where the record is shape-checked — a 409 carrying only `{detail}` is not a
            // resolution and must not fire it.
            announceActionResolved(rec);
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

  // A Pulse card is itself an `<li>`, so an embedded row nested one list item inside another —
  // `<ul><li class="card"><li class="act">…` — which is invalid, and exposes the action to
  // assistive technology as a second list item with no list. The standalone queue still IS a
  // list, so the element depends on where the row is rendered, not on how it looks.
  const Root = embedded ? "div" : "li";

  // The decision controls. Embedded they ride the evidence disclosure's head row so they cost no
  // extra line (#781); standalone they keep their own row under the evidence block.
  const controls = (approvable || rejectable) && (
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
          {/* An escalation offers NO Approve — there is nothing to deliver — so this is the
              row's only control, and beside the evidence disclosure a bare ✕ reads as "close
              that panel" rather than "settle this". Everywhere else Approve stands next to it
              and the pairing already says what it does, so the glyph stays on its own. */}
          {action.state === "escalated" && "Dismiss"}
        </button>
      )}
    </div>
  );
  // `none` renders no disclosure at all, so there would be no head row to ride.
  const inlineControls = embedded && action.evidence !== "none";

  return (
    <Root
      className={`${styles.act} ${toneOf(action)} ${embedded ? styles.actEmbedded : ""}`}
    >
      <div className={styles.actTop}>
        <span
          className={`${styles.verb} ${action.verb === "escalate" ? styles.verbEsc : ""}`}
        >
          {action.verb.toUpperCase()}
          {action.verb === "choose" && action.option !== undefined
            ? ` ${action.option}`
            : ""}
        </span>
        {/* The row carried its own identity because it used to stand alone in a queue. On a
            session card the card IS the identity, so repeating it renders the same title
            twice (#754). */}
        {!embedded && (
          <>
            <span className={styles.sname}>
              {action.title || action.session_id}
            </span>
            <span className={styles.eng} aria-hidden="true">
              {engineBadge(action.engine)}
            </span>
          </>
        )}
        <span className={styles.conf}>
          conf {action.confidence.toFixed(2)}
          {escSuffix ? ` · ${escSuffix}` : ""}
        </span>
      </div>
      {action.rationale && <p className={styles.why}>{action.rationale}</p>}
      {action.answer && <p className={styles.why}>{`“${action.answer}”`}</p>}
      {/* Evidence sits ABOVE the buttons on purpose: the operator should be able to see what
          the session is actually showing before they authorise typing into it. */}
      <EvidenceBlock
        sessionId={action.session_id}
        kind={action.evidence}
        trailing={inlineControls ? controls : undefined}
      />
      {note && <p className={styles.stale}>{note}</p>}
      {!inlineControls && controls}
      {/* Embedded, the card owns the footer (#781). This one repeated the session link the card
          already had and put a second clock (the action's age) beside the card's own — two
          footers for one session. The action's `state` is not dropped: the card folds it into
          its single footer, which already existed, so the merge costs no vertical space. */}
      {!embedded && (
        <div className={styles.actFoot}>
          {/* The feed no longer groups by project (#754), so the row has to say which one it is.
            Suppressed when embedded: a session card already names its project in its own
            footer, and repeating it there is noise. */}
          {action.project && (
            <span className={styles.proj}>{action.project}</span>
          )}
          <span className={styles.state}>{action.state}</span>
          {/* The feed is one row per session (#774). Say what was folded in, so a collapsed row
            is visibly a summary rather than looking like the only thing that happened. */}
          {(action.repeats ?? 1) > 1 && (
            <span
              className={styles.repeats}
              title={`${action.repeats} actions on this session`}
            >
              ×{action.repeats}
            </span>
          )}
          <span className={styles.age}>{relTime(action.ts)}</span>
          <Link className={styles.jump} to={sessionPath(action)}>
            Open session
          </Link>
        </div>
      )}
    </Root>
  );
}

/** Legacy grouping helper — the feed is one flat grid since #754. */
