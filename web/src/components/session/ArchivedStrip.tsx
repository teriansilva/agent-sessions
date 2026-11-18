import { useEffect, useRef } from "react";
import compose from "../terminal/Compose.module.css";
import type { UnarchiveState } from "./useArchivedSession";

/** The copy under the title, per pane: an API session resumes on the next send, a terminal session
 *  when its terminal opens again. */
const ARCHIVED_COPY = {
  api: "This session is frozen: no processes, no memory. Unarchive it to send a message — the agent resumes on your next send.",
  terminal:
    "This session is frozen: no processes, no memory. Unarchive it to reopen the terminal — the agent resumes when it opens.",
} as const;

const BUSY_COPY = {
  api: "Unarchiving… the composer comes back once the session reads active.",
  terminal: "Unarchiving… the terminal comes back once the session reads active.",
} as const;

/** Is focus somewhere the operator is typing (outside this strip)? Then the strip does not take it. */
function typingElsewhere(strip: HTMLElement | null): boolean {
  const el = document.activeElement as HTMLElement | null;
  if (!el || el === document.body || strip?.contains(el)) return false;
  return el.isContentEditable || el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement;
}

/** ARCHIVED · READ-ONLY (#2064): the strip that takes the compose bar's place while a session is
 *  archived, in the API-session pane and the terminal pane alike. One primary action, UNARCHIVE
 *  (a 44 px target), which takes focus when the strip appears. While it runs it reads
 *  UNARCHIVING… and is disabled; a failure says why in the danger text and offers RETRY, and the
 *  session stays read-only. Styles live with the compose bar (`Compose.module.css`): the strip is
 *  that bar's archived state. */
export function ArchivedStrip({
  pane,
  state,
  onUnarchive,
}: {
  pane: keyof typeof ARCHIVED_COPY;
  state: UnarchiveState;
  onUnarchive: () => void;
}) {
  const stripRef = useRef<HTMLElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  // Focus moves to UNARCHIVE when the strip appears — unless the operator is typing elsewhere (a
  // second pane, a map window's neighbour), whose caret it must not steal.
  useEffect(() => {
    if (!typingElsewhere(stripRef.current)) btnRef.current?.focus({ preventScroll: true });
  }, []);
  // A disabled button drops focus while it runs; once it is pressable again (RETRY), it gets it
  // back if nothing else took it meanwhile.
  const phase = state.phase;
  useEffect(() => {
    if (phase !== "failed") return;
    const el = document.activeElement;
    if (!el || el === document.body) btnRef.current?.focus({ preventScroll: true });
  }, [phase]);

  const busy = phase === "busy";
  return (
    <section
      ref={stripRef}
      className={compose.archived}
      aria-label="Archived session"
      data-testid="archived-strip"
      data-state={phase}
    >
      <div className={compose.archivedInner}>
        <div className={compose.archivedText}>
          <p className={compose.archivedTitle}>Archived · read-only</p>
          {state.phase === "failed" ? (
            <p className={`${compose.archivedCopy} ${compose.archivedError}`} role="alert">
              Couldn’t unarchive: {state.message}. Still archived.
            </p>
          ) : (
            <p className={compose.archivedCopy} role="status">
              {busy ? BUSY_COPY[pane] : ARCHIVED_COPY[pane]}
            </p>
          )}
        </div>
        <button
          ref={btnRef}
          type="button"
          className={compose.archivedBtn}
          onClick={onUnarchive}
          disabled={busy}
          aria-busy={busy || undefined}
          data-testid="archived-unarchive"
        >
          {busy ? "Unarchiving…" : phase === "failed" ? "Retry" : "Unarchive"}
        </button>
      </div>
    </section>
  );
}
