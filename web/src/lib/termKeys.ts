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
