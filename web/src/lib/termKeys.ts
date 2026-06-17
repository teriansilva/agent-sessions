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
