// Mouse-gesture arbitration for the terminal (#536 / #582 / #617).
//
// When an agent arms mouse tracking (?1000h/?1002h/?1003h) xterm hands every unmodified left-press
// to the app, so a plain drag selects nothing. #536 fixed that by re-dispatching the press as its
// force-selection twin — but only on the NORMAL buffer, to protect the clickable UI of alt-screen
// TUIs. That guarded the wrong axis: what must be preserved is the CLICK, not the buffer. Claude
// Code (>=2.1.178) and opencode both render in the alternate screen *with* mouse tracking, so the
// buffer guard disabled selection for exactly the agents that need it — and Ctrl+C, which only
// copies when a selection exists, fell through to the PTY as a SIGINT that killed the agent's turn.
//
// So: discriminate drag from click instead of buffer from buffer.
//   • plain press, mouse-tracking session → DEFER. Swallow it, remember the anchor.
//       – pointer moves past the slop → it was a DRAG → dispatch the force-select twin at the
//         anchor; xterm starts a fresh selection and extends it from the real mousemoves.
//       – pointer released inside the slop → it was a CLICK → replay a plain press so the TUI
//         receives it exactly as before.
//   • double/triple press (detail > 1) → force-select immediately: word/line selection must not
//     wait for a drag that will never come.
//   • no mouse tracking (codex, antigravity) → NATIVE. xterm already selects on a plain drag, and
//     a twin there becomes an empty Shift-incremental *extend* from a nonexistent anchor — the
//     regression #582 fixed. The buffer type is irrelevant to all of this.

/** Movement (px, Chebyshev) that separates a click from a drag. Small: a selection should start as
 *  soon as the pointer visibly moves, but a shaky click must not become an empty selection. */
export const DRAG_SLOP_PX = 4;

export type MouseDownDecision =
  /** Let the event reach xterm untouched (native selection, or our own synthetic replay). */
  | "native"
  /** Force a selection at once — double/triple click word/line select. */
  | "force-select"
  /** Swallow it; the drag-vs-click verdict comes from the following move/up. */
  | "defer";

export interface GestureEnv {
  /** The app owns the mouse (`term.modes.mouseTrackingMode !== "none"`). */
  mouseTracking: boolean;
}

export interface GestureMouseDown {
  isTrusted: boolean;
  button: number;
  detail: number;
  shiftKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  metaKey: boolean;
}

/** What to do with a `mousedown` on the terminal surface. */
export function decideMouseDown(
  e: GestureMouseDown,
  env: GestureEnv,
): MouseDownDecision {
  if (!e.isTrusted) return "native"; // a twin / click-replay we dispatched ourselves
  if (e.button !== 0) return "native"; // right/middle: context menu, paste — never selection
  if (e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return "native"; // explicit user intent
  if (!env.mouseTracking) return "native"; // xterm selects natively; a twin would break it (#582)
  return e.detail > 1 ? "force-select" : "defer";
}

/** True once the pointer has moved far enough from the anchor to mean "drag", not "click". */
export function exceededSlop(
  anchorX: number,
  anchorY: number,
  x: number,
  y: number,
  slop: number = DRAG_SLOP_PX,
): boolean {
  return Math.abs(x - anchorX) > slop || Math.abs(y - anchorY) > slop;
}

/** The modifier that makes xterm force a selection while the app owns the mouse.
 *
 *  xterm 5.5.0, `SelectionService.shouldForceSelection`:
 *      isMac ? e.altKey && rawOptions.macOptionClickForcesSelection : e.shiftKey
 *
 *  So Shift is inert on macOS and Alt is inert everywhere else — a Shift-only twin silently failed
 *  to select on every Mac. We only ever synthesize this modifier; the operator still just drags.
 *  The Mac branch additionally requires the terminal to be constructed with
 *  `macOptionClickForcesSelection: true` (see Terminal.tsx). */
export function forceSelectModifier(isMac: boolean): {
  shiftKey: boolean;
  altKey: boolean;
} {
  return { shiftKey: !isMac, altKey: isMac };
}
