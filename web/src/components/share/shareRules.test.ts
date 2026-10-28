import { describe, expect, test } from "vitest";
import type { ShareCatalogItem, SharePreviewItem } from "../../types/share";
import { asCatalog, asImports, asPreview, closure, previewStates } from "./shareRules";

const cat = (kind: string, id: string, depends_on: string[] = []): ShareCatalogItem => ({
  kind,
  id,
  title: id,
  depends_on,
  exportable: true,
  reason: null,
  source: "local",
});

const item = (
  key: string,
  depends_on: string[] = [],
  status: SharePreviewItem["status"] = "new",
): SharePreviewItem => {
  const [kind, id] = key.split(":");
  return { key, kind, id, title: id, dest: id, status, reason: status === "refused" ? "bad" : null, depends_on, uses: [], files: [] };
};

describe("export closure", () => {
  const items = [
    cat("skill", "a"),
    cat("skill", "b"),
    cat("playbook", "p", ["skill:a", "skill:b"]),
    cat("playbook", "q", ["skill:a"]),
  ];
  test("a chosen playbook includes its skills, each naming who needs it", () => {
    const inc = closure(items, new Set(["playbook:p", "playbook:q"]));
    expect(inc.get("skill:a")).toEqual(["playbook:p", "playbook:q"]);
    expect(inc.get("skill:b")).toEqual(["playbook:p"]);
  });
  test("a chosen item is never listed as included, and unticking releases the closure", () => {
    expect(closure(items, new Set(["playbook:p", "skill:a"])).has("skill:a")).toBe(false);
    expect(closure(items, new Set()).size).toBe(0);
  });
});

describe("preview states", () => {
  const items = [item("skill:a"), item("skill:b"), item("playbook:p", ["skill:a", "skill:b"]), item("playbook:x", [], "refused")];
  test("all selected: everything but a refused item imports", () => {
    const s = previewStates(items, new Set(["skill:a", "skill:b", "playbook:p"]));
    expect(s.get("playbook:p")).toEqual({ state: "import" });
    expect(s.get("playbook:x")?.state).toBe("refused");
  });
  test("deselecting a dependency refuses its dependent with the reason in words", () => {
    const s = previewStates(items, new Set(["skill:a", "playbook:p"]));
    expect(s.get("skill:b")).toEqual({ state: "deselected" });
    const p = s.get("playbook:p");
    expect(p?.state).toBe("refused");
    expect(p && "reason" in p && p.reason).toMatch(/needs skill b, which isn't being imported/i);
  });
  test("a refused dependency refuses its dependent", () => {
    const s = previewStates([item("skill:a", [], "refused"), item("playbook:p", ["skill:a"])], new Set(["playbook:p"]));
    const p = s.get("playbook:p");
    expect(p && "reason" in p && p.reason).toMatch(/can't be imported/);
  });
});

describe("shape guards (an e2e stub answers {})", () => {
  test("catalog, imports and preview refuse the wrong shape", () => {
    expect(asCatalog({})).toBeNull();
    expect(asCatalog({ items: [{ kind: "skill", id: "a", depends_on: [] }, { bogus: 1 }] })?.items).toHaveLength(1);
    expect(asImports([])).toEqual({});
    expect(asImports({ "skill:a": { bundle: "d", revision: 3 } })).toEqual({ "skill:a": { bundle: "d", revision: null } });
    expect(asPreview({})).toBeNull();
  });
});
