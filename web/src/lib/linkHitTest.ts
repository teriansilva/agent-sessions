// Wrapped-aware URL hit-test for the touch tap path (#664). The coarse-pointer overlay
// swallows taps before xterm's WebLinksAddon can see them (#415), so the tap handler
// re-implements the link lookup itself — and a single-row lookup breaks every URL that
// soft-wraps: the first row matches only a truncated fragment, a continuation row has
// no scheme prefix and matches nothing. xterm marks continuation rows with `isWrapped`,
// so join the logical line the way the addon does and hit-test the tapped cell against
// the joined text.

export const URL_RE = /\bhttps?:\/\/[^\s"'`<>]+/g;

/** Cap on rows joined into one logical line (same spirit as addon-web-links' own
 *  length limit). Past it the lookup gives up entirely — opening a URL that merely
 *  LOOKS complete is worse than opening nothing. */
export const MAX_JOIN_ROWS = 8;

/** Structural slice of xterm's IBuffer/IBufferLine, so the helper stays framework-free
 *  and unit-testable against synthetic buffers. */
export interface RowLike {
  readonly isWrapped: boolean;
  translateToString(trimRight?: boolean): string;
}
export interface BufferLike {
  getLine(y: number): RowLike | undefined;
}

/**
 * The URL under the tapped cell, joined across soft-wrapped rows — or null when the
 * cell isn't on a URL, or the logical line spans more than MAX_JOIN_ROWS rows.
 *
 * `absRow` is the absolute buffer row (viewportY + viewport row); `cols` the terminal
 * width. Rows are joined untrimmed so the tapped cell's index into the joined string
 * stays exact; the padding spaces can only terminate a URL match, never extend it.
 */
export function urlAtCell(
  buf: BufferLike,
  absRow: number,
  col: number,
  cols: number,
): string | null {
  if (cols <= 0 || col < 0 || !buf.getLine(absRow)) return null;
  // Walk back to the logical-line start: `isWrapped` marks a row as the continuation
  // of the row ABOVE it, so the start is the nearest row that isn't wrapped.
  let start = absRow;
  while (start > 0 && buf.getLine(start)?.isWrapped) {
    start--;
    if (absRow - start >= MAX_JOIN_ROWS) return null;
  }
  let text = "";
  let index = -1;
  for (let row = start; ; row++) {
    if (row - start >= MAX_JOIN_ROWS) return null; // line keeps wrapping past the cap
    const line = buf.getLine(row);
    if (!line) break;
    const chunk = line.translateToString(false);
    if (row === absRow) {
      if (col >= chunk.length) return null; // tapped right of the row's content
      index = text.length + col;
    }
    text += chunk;
    if (!buf.getLine(row + 1)?.isWrapped) break;
  }
  if (index < 0) return null;
  for (const m of text.matchAll(URL_RE)) {
    const s = m.index ?? 0;
    if (index >= s && index < s + m[0].length) return m[0];
  }
  return null;
}
