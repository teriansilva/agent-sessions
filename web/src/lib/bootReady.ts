// Fresh-launch input-ready gate (#533 / #607 / #616).
//
// Input written into a still-booting agent is swallowed (the composed text) or mis-submitted (the
// literal Ctrl-A of the line-clear). So a fresh launch holds the first compose send until the boot
// stream says the agent's input loop is live. Deciding *when* that is has been wrong twice:
//
//   #533 — ready on the bracketed-paste enable (ESC[?2004h), else a fallback timer started at the
//          first output byte. Codex never emits ?2004h and kept painting long after that timer.
//   #607 — restart the fallback on every chunk, so it means "output went quiet". Fixed Codex.
//   #616 — but ?2004h was STILL an instant ready, and Claude Code (>=2.1.178) emits it during its
//          pre-TUI terminal setup — at byte ~25, before it switches to the alternate screen
//          (ESC[?1049h) and CLEARS it (ESC[2J) at byte ~65. Measured across 39 fresh-claude boot
//          rings, 38 emit ?2004h before the clear. So the paste landed on a screen the agent was
//          about to wipe: message gone, composer and server draft already cleared.
//
// Hence: ?2004h is evidence the input plumbing is armed, never that the first paint has settled.
// Readiness is always "output has gone quiet"; ?2004h only shortens how long quiet must last.

/** Quiet window when the agent never armed bracketed paste (Codex, gemini) — it may still be
 *  painting boot/progress frames, so give it a generous beat. */
export const READY_QUIET_MS = 1500;

/** Quiet window once ESC[?2004h has been seen: the input plumbing is up, so we only wait out the
 *  in-flight paint (the alt-screen switch, the clear, the banner). */
export const READY_QUIET_AFTER_PASTE_ENABLE_MS = 400;

/** Ceiling from the first output byte. An agent that never goes quiet (a spinner, a clock) must
 *  still become ready — well inside Compose's READY_WAIT_MS (20s) "not sent" abort. */
export const READY_MAX_MS = 10_000;

/** The bracketed-paste enable, and the carry needed to spot it split across two chunks. */
const PASTE_ENABLE = "\x1b[?2004h";
const CARRY = PASTE_ENABLE.length - 1;

export interface BootReadyGate {
  /** Feed one chunk of raw boot output. Calls `onReady` (once) when the agent looks writable. */
  note(chunk: Uint8Array): void;
  /** Drop pending timers; `onReady` can never fire afterwards. */
  dispose(): void;
}

/** Watch a fresh session's boot stream and fire `onReady` once its input is live.
 *  Byte-oriented and resumable: a `PASTE_ENABLE` split across chunk boundaries is still seen. */
export function createBootReadyGate(onReady: () => void): BootReadyGate {
  const latin1 = new TextDecoder("latin1");
  let carry = "";
  let pasteEnableSeen = false;
  let done = false;
  let quietTimer: ReturnType<typeof setTimeout> | null = null;
  let ceilingTimer: ReturnType<typeof setTimeout> | null = null;

  const clearTimers = () => {
    if (quietTimer !== null) clearTimeout(quietTimer);
    if (ceilingTimer !== null) clearTimeout(ceilingTimer);
    quietTimer = ceilingTimer = null;
  };

  const fire = () => {
    if (done) return;
    done = true;
    clearTimers();
    onReady();
  };

  return {
    note(chunk) {
      if (done) return;
      // Scan BEFORE arming the timer, so a chunk carrying ?2004h re-arms with the short window.
      const hay = carry + latin1.decode(chunk);
      if (!pasteEnableSeen && hay.includes(PASTE_ENABLE)) pasteEnableSeen = true;
      carry = hay.slice(-CARRY);

      // The ceiling runs from the first byte and is never restarted; the quiet window restarts on
      // every chunk, so it measures the gap since the agent last painted.
      if (ceilingTimer === null) ceilingTimer = setTimeout(fire, READY_MAX_MS);
      if (quietTimer !== null) clearTimeout(quietTimer);
      quietTimer = setTimeout(
        fire,
        pasteEnableSeen ? READY_QUIET_AFTER_PASTE_ENABLE_MS : READY_QUIET_MS,
      );
    },
    dispose() {
      done = true;
      clearTimers();
    },
  };
}
