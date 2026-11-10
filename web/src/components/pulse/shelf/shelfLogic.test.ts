import { describe, expect, test } from "vitest";

import type { ShelfItem } from "../../../types/shelf";
import {
  NO_FILTERS,
  applyFilters,
  defaultView,
  fmtBytes,
  fmtDuration,
  footState,
  groupBySession,
  isInlineMedia,
  isTextDocument,
  shortSha,
  tabCounts,
  withItem,
} from "./shelfLogic";

function item(over: Partial<ShelfItem>): ShelfItem {
  return {
    id: "shf_x",
    seq: 1,
    source: "delivered",
    kind: "text",
    title: "t",
    why: "",
    session_key: "claude:a",
    ref_type: "text",
    size: 1,
    sha256: null,
    created_at: 0,
    verdict: null,
    note: "",
    verdict_at: null,
    ...over,
  };
}

describe("the shelf's pure rules", () => {
  test("filters narrow by kind, source and review state together", () => {
    const items = [
      item({ id: "a", kind: "image" }),
      item({ id: "b", kind: "image", verdict: "good" }),
      item({ id: "c", kind: "link", source: "found" }),
    ];
    expect(applyFilters(items, { ...NO_FILTERS, kind: "image" }).map((i) => i.id)).toEqual(["a", "b"]);
    expect(applyFilters(items, { ...NO_FILTERS, source: "found" }).map((i) => i.id)).toEqual(["c"]);
    expect(
      applyFilters(items, { ...NO_FILTERS, kind: "image", unreviewedOnly: true }).map((i) => i.id),
    ).toEqual(["a"]);
  });

  test("groups keep arrival order and each item's own order", () => {
    const g = groupBySession([
      item({ id: "1", session_key: "codex:b" }),
      item({ id: "2", session_key: "claude:a" }),
      item({ id: "3", session_key: "codex:b" }),
      item({ id: "4", session_key: null }),
    ]);
    expect(g.map((x) => [x.key, x.items.map((i) => i.id)])).toEqual([
      ["codex:b", ["1", "3"]],
      ["claude:a", ["2"]],
      [null, ["4"]],
    ]);
  });

  test("a gone file is never handed to a viewer; a download-only one is not inline", () => {
    const img = item({ kind: "image", ref_type: "path", mime: "image/png", file: { state: "ok" } });
    expect(isInlineMedia(img)).toBe(true);
    expect(isInlineMedia({ ...img, file: { state: "missing" } })).toBe(false);
    expect(isInlineMedia({ ...img, file: { state: "inaccessible" } })).toBe(false);
    expect(isInlineMedia({ ...img, mime: null })).toBe(false);
    const doc = item({ kind: "document", ref_type: "path", mime: null, file: { state: "ok" } });
    expect(isTextDocument(doc)).toBe(false);
    expect(isTextDocument({ ...doc, mime: "text/plain; charset=utf-8" })).toBe(true);
  });

  test("the tile foot always says its state in words", () => {
    expect(footState(item({ verdict: "good" })).word).toBe("Looks good");
    expect(footState(item({ verdict: "needs_work" }))).toMatchObject({ word: "Needs work", led: "attention" });
    expect(footState(item({}))).toMatchObject({ word: "Unreviewed", led: "idle" });
    expect(footState(item({ ref_type: "path", file: { state: "missing" } })).word).toBe("Missing");
  });

  test("formats are honest about unknowns", () => {
    expect(fmtBytes(null)).toBe("—");
    expect(fmtBytes(612 * 1024)).toBe("612 KB");
    expect(fmtBytes(18.4 * 1024 * 1024)).toBe("18.4 MB");
    expect(fmtDuration(31)).toBe("00:31");
    expect(fmtDuration(Number.NaN)).toBe("");
    expect(shortSha("9f2c" + "0".repeat(56) + "41a0")).toBe("9f2c…41a0");
  });

  test("review and closed missions open on the shelf, everything else on the thread", () => {
    for (const s of ["review", "done", "failed", "abandoned"]) expect(defaultView(s)).toBe("shelf");
    for (const s of ["draft", "planned", "dispatching", "running", undefined]) {
      expect(defaultView(s)).toBe("thread");
    }
  });

  test("a verdict answer replaces its item in place and keeps the file state read last", () => {
    const before = [item({ id: "a", ref_type: "path", file: { state: "changed" } }), item({ id: "b" })];
    const next = withItem(before, item({ id: "a", verdict: "good", ref_type: "path" }));
    expect(next[0].verdict).toBe("good");
    expect(next[0].file).toEqual({ state: "changed" });
    expect(tabCounts(next)).toEqual({ total: 2, toReview: 1 });
  });
});
