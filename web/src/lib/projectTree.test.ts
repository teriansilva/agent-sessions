import { expect, test } from "vitest";
import { buildProjectTree, flattenTree, nearestAncestor, owningProjectId } from "./projectTree";

// nearestAncestor — the core boundary-aware rule (#148 carry-over, #174 extract).

test("nearestAncestor: parent linked by folder boundary, not by string prefix", () => {
  const present = new Set(["/a", "/a/b", "/a-foo", "/c"]);
  expect(nearestAncestor("/a/b", present)).toBe("/a");
  // `/a-foo` is NOT under `/a` even though the strings share a prefix — boundary check.
  expect(nearestAncestor("/a-foo", present)).toBeUndefined();
  // Disconnected → no parent.
  expect(nearestAncestor("/c", present)).toBeUndefined();
});

test("nearestAncestor: picks the longest match when multiple ancestors are present", () => {
  const present = new Set(["/a", "/a/b", "/a/b/c"]);
  expect(nearestAncestor("/a/b/c", present)).toBe("/a/b"); // not /a
});

test("nearestAncestor: skips absent intermediates (no synthetic nodes)", () => {
  const present = new Set(["/a", "/a/b/c/d"]);
  expect(nearestAncestor("/a/b/c/d", present)).toBe("/a");
});

// owningProjectId — the client-side mirror of the server resolver's folder step (#361).

test("owningProjectId: boundary-aware, not a string prefix", () => {
  const entities = [{ id: "p-a", folders: ["/a"] }];
  expect(owningProjectId("/a", entities)).toBe("p-a"); // exact match
  expect(owningProjectId("/a/b/c", entities)).toBe("p-a"); // nested
  expect(owningProjectId("/a-foo", entities)).toBe(""); // sibling sharing a string prefix
  expect(owningProjectId("/elsewhere", entities)).toBe("");
  expect(owningProjectId("", entities)).toBe("");
});

test("owningProjectId: the most specific adopted folder wins across entities", () => {
  const entities = [
    { id: "p-outer", folders: ["/a"] },
    { id: "p-inner", folders: ["/a/b"] },
  ];
  expect(owningProjectId("/a/b/c", entities)).toBe("p-inner");
  expect(owningProjectId("/a/z", entities)).toBe("p-outer");
});

test("owningProjectId: an entity with no folders never owns anything", () => {
  expect(owningProjectId("/a", [{ id: "p-x", folders: [] }])).toBe("");
});

// buildProjectTree — the resolved map both surfaces will consume.

test("buildProjectTree: parents/children/depth are consistent", () => {
  const cwds = ["/a", "/a/b", "/a/b/c", "/x"];
  const t = buildProjectTree(cwds);
  expect(t.get("/a")!.parent).toBeUndefined();
  expect(t.get("/a")!.depth).toBe(0);
  expect(t.get("/a/b")!.parent).toBe("/a");
  expect(t.get("/a/b")!.depth).toBe(1);
  expect(t.get("/a/b/c")!.parent).toBe("/a/b");
  expect(t.get("/a/b/c")!.depth).toBe(2);
  expect(t.get("/x")!.parent).toBeUndefined();
  expect(t.get("/x")!.depth).toBe(0);
  // Children list mirrors the parent relation.
  expect(t.get("/a")!.children).toEqual(["/a/b"]);
  expect(t.get("/a/b")!.children).toEqual(["/a/b/c"]);
  expect(t.get("/x")!.children).toEqual([]);
});

test("buildProjectTree: multiple roots remain disconnected", () => {
  const t = buildProjectTree(["/x", "/y", "/x/a", "/y/b"]);
  expect([...t.values()].filter((n) => n.parent === undefined).map((n) => n.cwd).sort()).toEqual([
    "/x",
    "/y",
  ]);
});

// flattenTree — DFS for rendering as a list.

test("flattenTree: depth-first with stable child sort", () => {
  const t = buildProjectTree(["/a", "/a/b", "/a/c", "/x"]);
  const flat = flattenTree(t).map((n) => n.cwd);
  expect(flat).toEqual(["/a", "/a/b", "/a/c", "/x"]); // alphabetical by default
});

test("flattenTree: caller-provided sort is honored", () => {
  const t = buildProjectTree(["/a/foo", "/a/bar", "/a"]);
  // Reverse alphabetical — verifies the comparator is applied at every level + at roots.
  const flat = flattenTree(t, (a, b) => b.localeCompare(a)).map((n) => n.cwd);
  expect(flat).toEqual(["/a", "/a/foo", "/a/bar"]);
});
