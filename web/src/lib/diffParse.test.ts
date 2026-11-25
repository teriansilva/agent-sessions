import { describe, expect, it } from "vitest";
import { countChanges, parseDiff } from "./diffParse";

describe("unified diff parsing (#784)", () => {
  it("numbers both gutters across a hunk", () => {
    const hunks = parseDiff(
      ["@@ -10,3 +10,4 @@ def f():", " keep", "-gone", "+added", "+also", " tail"].join("\n"),
    );
    expect(hunks).toHaveLength(1);
    expect(hunks[0].lines.map((l) => [l.kind, l.oldNo, l.newNo])).toEqual([
      ["context", 10, 10],
      ["del", 11, null],
      ["add", null, 11],
      ["add", null, 12],
      ["context", 12, 13],
    ]);
  });

  it("handles several hunks and restarts the numbering at each header", () => {
    const hunks = parseDiff(
      ["@@ -1,1 +1,1 @@", "-a", "+b", "@@ -50,1 +60,1 @@", "-x", "+y"].join("\n"),
    );
    expect(hunks).toHaveLength(2);
    expect(hunks[1].lines[0]).toMatchObject({ kind: "del", oldNo: 50 });
    expect(hunks[1].lines[1]).toMatchObject({ kind: "add", newNo: 60 });
  });

  it("treats '\\ No newline at end of file' as numbering nothing", () => {
    const hunks = parseDiff(["@@ -1,1 +1,1 @@", "-a", "\\ No newline at end of file", "+a"].join("\n"));
    const kinds = hunks[0].lines.map((l) => l.kind);
    expect(kinds).toEqual(["del", "nonewline", "add"]);
    expect(hunks[0].lines[1]).toMatchObject({ oldNo: null, newNo: null });
  });

  it("keeps \\r so a CRLF file does not read as every line changed", () => {
    const hunks = parseDiff(["@@ -1,1 +1,1 @@", "-a\r", "+b\r"].join("\n"));
    expect(hunks[0].lines[0].text).toBe("a\r");
  });

  it("treats a bare empty line as empty context, not as a dropped line", () => {
    const hunks = parseDiff(["@@ -1,3 +1,3 @@", " a", "", " b"].join("\n"));
    expect(hunks[0].lines.map((l) => l.kind)).toEqual(["context", "context", "context"]);
  });

  it("ignores file headers before the first hunk", () => {
    const hunks = parseDiff(["--- a/x", "+++ b/x", "@@ -1,1 +1,1 @@", "-a", "+b"].join("\n"));
    expect(hunks).toHaveLength(1);
    expect(hunks[0].lines).toHaveLength(2);
  });

  it("returns nothing for empty or unparseable input rather than throwing", () => {
    expect(parseDiff("")).toEqual([]);
    expect(parseDiff("not a diff at all")).toEqual([]);
  });

  it("counts changes for a complete diff", () => {
    const hunks = parseDiff(["@@ -1,2 +1,3 @@", " a", "-b", "+c", "+d"].join("\n"));
    expect(countChanges(hunks)).toEqual({ added: 2, removed: 1 });
  });
});
