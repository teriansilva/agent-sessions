// Read a headless xterm buffer's rows as replayable ANSI — the "custom reflowed-row snapshot"
// chosen in Phase 0 (#272). Unlike SerializeAddon's wrap-continuity encoding (which only round-trips
// narrow reflowed content 83–95%), this emits each buffer row as ONE literal line with its SGR runs,
// so a client writing it reproduces exactly what this emulator reflowed — byte-perfect at any width.
//
// The caller resizes the terminal to the client width FIRST (feed-then-resize), then calls this.

const FG_DEFAULT = "39";
const BG_DEFAULT = "49";

// IBufferCell color: use the unambiguous boolean predicates, NOT getFg/BgColorMode(). The mode
// getters return xterm's RAW masked bit-flags (CM_P16=0x01000000, CM_P256=0x02000000,
// CM_RGB=0x03000000) — not 0/1/2 — so comparing `mode === 2` silently misclassifies every RGB cell
// as palette and emits its 24-bit value as a bogus `38;5;<huge>` index → garbled colors (#273).
function fgParams(cell) {
  if (cell.isFgDefault()) return FG_DEFAULT;
  const c = cell.getFgColor();
  if (cell.isFgRGB()) return `38;2;${(c >> 16) & 0xff};${(c >> 8) & 0xff};${c & 0xff}`;
  if (c < 8) return String(30 + c);
  if (c < 16) return String(90 + (c - 8));
  return `38;5;${c}`;
}

function bgParams(cell) {
  if (cell.isBgDefault()) return BG_DEFAULT;
  const c = cell.getBgColor();
  if (cell.isBgRGB()) return `48;2;${(c >> 16) & 0xff};${(c >> 8) & 0xff};${c & 0xff}`;
  if (c < 8) return String(40 + c);
  if (c < 16) return String(100 + (c - 8));
  return `48;5;${c}`;
}

// A stable key for a cell's style, so we only emit a new SGR when the style actually changes.
function styleKey(cell) {
  return [
    fgParams(cell),
    bgParams(cell),
    cell.isBold(),
    cell.isDim(),
    cell.isItalic(),
    cell.isUnderline(),
    cell.isInverse(),
    cell.isStrikethrough(),
  ].join(",");
}

function sgrFor(cell) {
  const p = [];
  if (cell.isBold()) p.push("1");
  if (cell.isDim()) p.push("2");
  if (cell.isItalic()) p.push("3");
  if (cell.isUnderline()) p.push("4");
  if (cell.isInverse()) p.push("7");
  if (cell.isStrikethrough()) p.push("9");
  p.push(fgParams(cell), bgParams(cell));
  return `\x1b[0;${p.join(";")}m`;
}

// Whether a cell is "blank with default style" — used to trim trailing run on each row.
function isBlankDefault(cell) {
  const ch = cell.getChars();
  return (
    (ch === "" || ch === " ") &&
    cell.isFgDefault() &&
    cell.isBgDefault() &&
    !cell.isBold() &&
    !cell.isDim() &&
    !cell.isItalic() &&
    !cell.isUnderline() &&
    !cell.isInverse() &&
    !cell.isStrikethrough()
  );
}

// Render one buffer line to a styled ANSI string (no trailing newline). When `full` is false the
// trailing default-blank cells are dropped (smaller payload, matches the rstrip the client tolerates).
// When `full` is true (a soft-wrap CONTINUED row) we emit all `cols` cells so the wrap boundary is
// preserved exactly where the emulator placed it — the next row's content joins flush against it.
function renderLine(line, cols, full = false) {
  let last;
  if (full) {
    last = cols - 1; // a wrapped-from row is full to the edge; keep every column
  } else {
    last = -1;
    const probe = line.getCell(0);
    for (let x = 0; x < cols; x++) {
      const c = line.getCell(x, probe);
      if (c && !isBlankDefault(c)) last = x;
    }
    if (last < 0) return "";
  }
  let out = "";
  let curKey = null;
  const cell = line.getCell(0);
  for (let x = 0; x <= last; x++) {
    const c = line.getCell(x, cell);
    if (!c) continue;
    if (c.getWidth() === 0) continue; // spacer cell after a wide glyph
    const k = styleKey(c);
    if (k !== curKey) {
      out += sgrFor(c);
      curKey = k;
    }
    const ch = c.getChars();
    out += ch === "" ? " " : ch;
  }
  return out + "\x1b[0m";
}

// Serialize the whole active buffer (scrollback + screen) to replayable ANSI. CRITICAL (#273): a
// soft-wrapped visual row (`line.isWrapped`) is emitted as a CONTINUATION of the previous row with NO
// `\r\n` between them — so the client's xterm sees one LOGICAL line and re-wraps it at ITS current
// width. Joining every row with a hard `\r\n` instead (the old behavior) froze the scroll-up at the
// capture width: when the terminal later widened, live content re-rendered wide but the scroll-up
// stayed narrow ("loaded with a different screen size"). Only a true line break (the next row is NOT
// wrapped) emits `\r\n`. A row that is continued by the next is rendered `full` so its wrap column is
// preserved.
export function snapshotRows(term) {
  const buf = term.buffer.active;
  const cols = term.cols;
  const n = buf.length;
  const parts = [];
  for (let i = 0; i < n; i++) {
    const line = buf.getLine(i);
    if (!line) {
      parts.push({ text: "", wrapped: false });
      continue;
    }
    const next = i + 1 < n ? buf.getLine(i + 1) : null;
    const continued = !!(next && next.isWrapped); // this row is wrapped TO the next → render full
    parts.push({ text: renderLine(line, cols, continued), wrapped: !!line.isWrapped });
  }
  // Drop trailing blank LOGICAL lines (a blank row that isn't itself a wrap-continuation).
  while (parts.length && parts[parts.length - 1].text === "" && !parts[parts.length - 1].wrapped) {
    parts.pop();
  }
  let out = "";
  for (let i = 0; i < parts.length; i++) {
    if (i > 0 && !parts[i].wrapped) out += "\r\n"; // new logical line; wrapped rows join flush
    out += parts[i].text;
  }
  return out;
}
