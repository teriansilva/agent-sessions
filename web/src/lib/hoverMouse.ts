// Hover-only mouse reports never reach the PTY (#1285).
//
// codex ≥ 0.160 arms any-motion mouse tracking (?1003h + SGR ?1006h), so xterm.js emits an
// `ESC [< 35 ; x ; y M` report for EVERY pointer move over the pane. Input travels through
// `dtach -a`, which forwards it in 8-byte packets the master writes to the agent separately; a
// read that ends on a lone ESC is parsed by codex as the Esc key — interrupting the turn — and
// the rest of the report lands in the composer as text (`[<35;51;59M`). No agent we ship uses
// hover, so the cheapest cure is to never send it. Clicks, drags and wheel still go through.

// A complete SGR mouse report: ESC [ < button ; col ; row (M press/motion | m release).
// eslint-disable-next-line no-control-regex -- matching the literal ESC that opens the report
const SGR_MOUSE = /\x1b\[<(\d+);\d+;\d+([Mm])/g;

/** Motion (32) with "no button" (3), under any Shift(4)/Alt(8)/Ctrl(16) — 35,39,…,63. Wheel and
 *  extended buttons (≥ 64) and drag motion (32–34 + modifiers) are not hover. */
export function isHoverButton(b: number): boolean {
  return b < 64 && (b & ~(4 | 8 | 16)) === 35;
}

/** `data` with every complete hover-only SGR report removed; every other byte (keys, a lone Esc,
 *  incomplete escapes, presses, releases, drags, wheel) is kept unchanged and in order. Nothing
 *  is buffered, so no keystroke waits on a report that might follow. */
export function stripHoverMouseReports(data: string): string {
  if (!data.includes("\x1b[<")) return data;
  return data.replace(SGR_MOUSE, (whole, b: string, final: string) =>
    final === "M" && isHoverButton(Number(b)) ? "" : whole,
  );
}
