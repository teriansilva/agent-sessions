import { useCallback, useEffect, useRef, useState } from "react";
import { ACTION_RESOLVED_EVENT } from "../../lib/actionEvents";
import { api } from "../../lib/api";
import { OPERATOR_PENDING } from "../../lib/orchestratorAction";
import type { OrchestratorAction } from "../../types/api";
import { ActionRow } from "../pulse/ActionRow";
import styles from "./Terminal.module.css";

const POLL_MS = 60_000;

/** Mission control's pending decisions for ONE session, in that session's own pane (#948 P3).
 *
 *  Before #948 a decision for a session no mission held had exactly one surface: the "Sessions
 *  without a mission" view in the Missions section. That view is gone, so the decision comes to
 *  the session — which is also where the bell and push notifications already link. It renders for
 *  any session with a pending action, held or not; a held session's decision additionally appears
 *  in its mission's thread, as before.
 *
 *  The controls are `ActionRow`'s, so what is offered is the SERVER's projection
 *  (`project_for_operator`) and never re-derived here. `embedded`, because the pane already names
 *  the session. The read is `GET /api/pulse/orchestrator`'s `pending` list, matched on the key the
 *  server acts on; it refreshes on mount, whenever an action is resolved anywhere in the tab, and on
 *  the bell's 60s cadence. A failed read keeps what was last shown rather than blanking the strip.
 *
 *  **A refusal outlives its row** (#959 review 4805, finding 2). A 409 carries the settled record,
 *  which removes the action — so when it was the session's LAST pending one, gating the whole
 *  strip on rows took the "Not sent — …" explanation down with it, and a refused approval looked
 *  exactly like a delivered one. The note is rendered on its own and the strip stays for it. It
 *  belongs to the session it was raised on, is replaced by the next refusal, and clears when a
 *  later decision here succeeds (so it cannot be read as that decision's outcome). A re-read never
 *  clears it: what the ledger says now is not an answer to what happened to the tap.
 */
export function SessionDecisions({
  sessionKey,
  reviewReason,
}: {
  sessionKey: string;
  /** The AI review's "needs a human" reason for this session, when it has one. Shown ONLY when an
   *  action carries no rationale of its own, so a decision never offers controls with no
   *  explanation at all — and never repeats the same reason twice (#781). */
  reviewReason?: string;
}) {
  const [pending, setPending] = useState<OrchestratorAction[]>([]);
  const [verbs, setVerbs] = useState<Set<string> | undefined>(undefined);
  /** Keyed on the session it was raised for, so it never shows on another session's pane. */
  const [note, setNote] = useState<{ key: string; text: string } | null>(null);
  /** Set by a refusal, consumed by the resolve that `ActionRow` issues in the same tick — which is
   *  how `onResolved` tells "this record came with a refusal" from a later successful decision. */
  const refusalPending = useRef(false);
  if (note && note.key !== sessionKey) setNote(null);
  // Every read carries a generation, so a slow earlier read cannot replace a newer one — and a
  // read issued for the previous session cannot land on this one.
  const gen = useRef(0);

  const load = useCallback(() => {
    const mine = ++gen.current;
    // NEVER ALLOWED TO TAKE THE PANE DOWN. This strip is an add-on to the terminal: a read that
    // throws synchronously (no such endpoint in an older client, a partial API double) or rejects
    // must leave the terminal exactly as it was, with the last shown decisions — so both paths are
    // caught here rather than trusting the promise alone.
    try {
      api
        .orchestrator()
        .then((s) => {
          if (mine !== gen.current) return;
          setPending((s.pending ?? []).filter((a) => a.session_id === sessionKey));
          setVerbs(s.delivering_verbs ? new Set(s.delivering_verbs) : undefined);
        })
        .catch(() => {});
    } catch {
      // keep the last shown decisions
    }
  }, [sessionKey]);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    window.addEventListener(ACTION_RESOLVED_EVENT, load);
    return () => {
      clearInterval(timer);
      window.removeEventListener(ACTION_RESOLVED_EVENT, load);
      gen.current += 1;
    };
  }, [load]);

  /** A decision settled HERE is applied from the server's response BEFORE the re-read (#762's rule,
   *  on this surface). The re-read can fail, and a failed read must not leave a settled action on
   *  screen still offering Approve. The generation bump in `load` stops an older in-flight read from
   *  putting it back. */
  const onResolved = useCallback(
    (settled: OrchestratorAction) => {
      setPending((prev) =>
        OPERATOR_PENDING.has(settled.state)
          ? prev.map((a) => (a.id === settled.id ? settled : a))
          : prev.filter((a) => a.id !== settled.id),
      );
      if (refusalPending.current) refusalPending.current = false;
      else setNote(null);
      load();
    },
    [load],
  );

  const onNote = useCallback(
    (text: string) => {
      refusalPending.current = true;
      setNote({ key: sessionKey, text });
    },
    [sessionKey],
  );

  const shownNote = note?.key === sessionKey ? note.text : null;
  if (pending.length === 0 && !shownNote) return null;
  return (
    <div
      className={styles.decisions}
      role="region"
      aria-label="Decisions waiting on you"
      data-testid="session-decisions"
    >
      {pending.map((a) => (
        <div key={a.id} className={styles.decisionItem}>
          <ActionRow
            action={a}
            embedded
            deliveringVerbs={verbs}
            onResolved={onResolved}
            onNote={onNote}
          />
          {!a.rationale?.trim() && reviewReason ? (
            <div className={styles.decisionReason}>{reviewReason}</div>
          ) : null}
          {/* `embedded` drops ActionRow's own footer on the promise that its host carries the
              state — so the host does, exactly once (#781). */}
          <div className={styles.decisionState} data-testid="session-state">
            {a.state}
          </div>
        </div>
      ))}
      {shownNote ? (
        <div className={styles.decisionsNote} role="status">
          {shownNote}
        </div>
      ) : null}
    </div>
  );
}
