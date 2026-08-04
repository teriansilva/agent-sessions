import { describe, expect, test } from "vitest";
import { MAX_JOIN_ROWS, urlAtCell, type BufferLike } from "./linkHitTest";

// Synthetic xterm buffer: each logical line is split into `cols`-wide rows, with every
// row after the first flagged isWrapped — exactly how xterm reflows long output.
function bufOf(logical: string[], cols: number): BufferLike {
  const rows: { text: string; wrapped: boolean }[] = [];
  for (const line of logical) {
    if (line.length === 0) {
      rows.push({ text: "", wrapped: false });
      continue;
    }
    for (let i = 0; i < line.length; i += cols) {
      rows.push({ text: line.slice(i, i + cols), wrapped: i > 0 });
    }
  }
  return {
    getLine(y: number) {
      const r = rows[y];
      if (!r) return undefined;
      return {
        isWrapped: r.wrapped,
        translateToString: (trimRight?: boolean) =>
          trimRight ? r.text.replace(/ +$/, "") : r.text.padEnd(cols),
      };
    },
  };
}

const COLS = 20;
const URL2 = "https://example.com/attachments/abc"; // 35 chars → 2 rows at 20 cols
const URL3 = "https://example.com/attachments/abcdefghijkl"; // 45 chars → 3 rows

describe("urlAtCell", () => {
  test("single-row URL still hits (regression) and misses off-URL cells", () => {
    const buf = bufOf(["https://ex.com/a done"], 40);
    expect(urlAtCell(buf, 0, 3, 40)).toBe("https://ex.com/a");
    expect(urlAtCell(buf, 0, 15, 40)).toBe("https://ex.com/a");
    expect(urlAtCell(buf, 0, 18, 40)).toBeNull(); // on " done"
  });

  test("URL spanning 2 rows: full URL from either row", () => {
    const buf = bufOf([URL2], COLS);
    expect(urlAtCell(buf, 0, 5, COLS)).toBe(URL2); // first row
    expect(urlAtCell(buf, 1, 10, COLS)).toBe(URL2); // continuation row
  });

  test("URL spanning 3 rows: first / middle / final row taps all hit; past the end misses", () => {
    const buf = bufOf([URL3], COLS);
    expect(urlAtCell(buf, 0, 0, COLS)).toBe(URL3); // very first cell
    expect(urlAtCell(buf, 1, 19, COLS)).toBe(URL3); // middle row, last cell
    expect(urlAtCell(buf, 2, 3, COLS)).toBe(URL3); // final row, last char of the 4-char remainder
    expect(urlAtCell(buf, 2, 4, COLS)).toBeNull(); // final row, just past the URL → keyboard
    expect(urlAtCell(buf, 2, 15, COLS)).toBeNull(); // final row, padding
  });

  test("leading text before the URL keeps the index math honest across the wrap", () => {
    const line = `fetch ${URL2} ok`;
    const buf = bufOf([line], COLS);
    expect(urlAtCell(buf, 0, 2, COLS)).toBeNull(); // on "fetch"
    expect(urlAtCell(buf, 0, 6, COLS)).toBe(URL2); // URL start
    expect(urlAtCell(buf, 1, 0, COLS)).toBe(URL2); // continuation
    expect(urlAtCell(buf, 2, 0, COLS)).toBe(URL2); // last URL char lands on row 2 (col 0)
    expect(urlAtCell(buf, 2, 3, COLS)).toBeNull(); // on " ok"
  });

  test("only the tapped logical line is considered — neighbours don't leak in", () => {
    const buf = bufOf(
      ["https://one.example/x", "plain text", "https://two.example/y"],
      40,
    );
    expect(urlAtCell(buf, 1, 3, 40)).toBeNull();
    expect(urlAtCell(buf, 2, 3, 40)).toBe("https://two.example/y");
  });

  test("logical lines past the row cap never open a truncated URL", () => {
    const over = `https://example.com/${"z".repeat(COLS * MAX_JOIN_ROWS)}`; // > MAX_JOIN_ROWS rows
    const buf = bufOf([over], COLS);
    expect(urlAtCell(buf, 0, 5, COLS)).toBeNull(); // start row
    expect(urlAtCell(buf, 4, 5, COLS)).toBeNull(); // middle row
    expect(urlAtCell(buf, MAX_JOIN_ROWS, 5, COLS)).toBeNull(); // deep continuation (backward cap)
  });

  test("a line of exactly MAX_JOIN_ROWS rows still works", () => {
    const exact = `https://ex.com/${"q".repeat(COLS * MAX_JOIN_ROWS - 15 - 3)}`; // fills MAX rows minus 3
    const buf = bufOf([exact], COLS);
    expect(exact.length).toBeLessThanOrEqual(COLS * MAX_JOIN_ROWS);
    expect(urlAtCell(buf, MAX_JOIN_ROWS - 1, 0, COLS)).toBe(exact);
  });

  test("out-of-range taps are null", () => {
    const buf = bufOf(["https://ex.com/a"], COLS);
    expect(urlAtCell(buf, 5, 0, COLS)).toBeNull(); // row past buffer
    expect(urlAtCell(buf, 0, -1, COLS)).toBeNull();
    expect(urlAtCell(buf, 0, 0, 0)).toBeNull();
  });
});
