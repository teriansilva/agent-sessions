// Control/navigation key sequences sent to the PTY as input (same set as the legacy
// client). The mobile action bar uses these so TUIs (claude's resume menu, opencode,
// etc.) are operable without a hardware keyboard.
export const KEYSEQ = {
  up: "\x1b[A",
  down: "\x1b[B",
  right: "\x1b[C",
  left: "\x1b[D",
  enter: "\r",
  esc: "\x1b",
  tab: "\t",
  ctrlc: "\x03",
  ctrlu: "\x15", // kill line
  ctrla: "\x01",
  ctrlk: "\x0b",
} as const;

export type KeyName = keyof typeof KEYSEQ;

/** Wrap text as a bracketed paste so the agent receives it as one paste, not
 *  per-keystroke input (no IME/autocomplete garble). */
export function bracketedPaste(text: string): string {
  return `\x1b[200~${text}\x1b[201~`;
}

/** The platform paste shortcut (Cmd+V on macOS, Ctrl+V elsewhere) on key DOWN (#209).
 *  We suppress it in xterm's custom key handler so the raw ``\x16`` keystroke never reaches
 *  the PTY — otherwise the agent (Claude Code) reads Ctrl+V as "paste image from clipboard"
 *  and prints "no image found in clipboard" on a text paste. The browser's native paste
 *  event still fires and is handled by Terminal's capture-phase listener. ``alt`` excluded
 *  so it never swallows unrelated control input. */
export function isPasteShortcut(
  e: { type: string; key: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean },
  isMac: boolean,
): boolean {
  if (e.type !== "keydown" || e.altKey) return false;
  if (e.key !== "v" && e.key !== "V") return false;
  return isMac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
}

/** Ctrl+C / Ctrl+Shift+C as a COPY shortcut on key DOWN (#536). Only meaningful while the
 *  terminal holds a selection — the CALLER checks that; without one Ctrl+C must stay the
 *  ``^C`` interrupt it always was. Meta/Alt combos are left alone: macOS Cmd+C already
 *  copies natively via xterm's mirrored DOM selection and never reaches the PTY, so it
 *  needs no interception on any platform. Keydown only — acting on keyup too would
 *  double-fire the copy. */
export function isCopyShortcut(e: {
  type: string;
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
}): boolean {
  if (e.type !== "keydown" || e.altKey || e.metaKey || !e.ctrlKey) return false;
  return e.key === "c" || e.key === "C";
}
