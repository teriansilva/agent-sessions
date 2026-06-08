import { expect, test } from "vitest";
import type { Session } from "../types/api";
import { ACTIVE_WINDOW_S, buildOverview } from "./overviewGraph";

const NOW = 1_700_000_000;

function s(over: Partial<Session> & { id: string }): Session {
  return {
    engine: "claude",
    uuid: over.id,
    short_uuid: over.id.slice(0, 6),
    cwd: "/home/u/proj",
    project: "proj",
    last_mtime: NOW,
    first_user_message: "",
    title: over.id,
    sticky: false,
    sort_key: 0,
    archived: false,
    ...over,
  } as Session;
}

/** Expand every cwd in the given sessions (chips only render for expanded clusters). */
const allExpanded = (sessions: Session[]) => new Set(sessions.map((x) => x.cwd));

test("groups sessions by cwd and emits one group node per project", () => {
  const { nodes } = buildOverview(
    [
      s({ id: "claude:a", cwd: "/p/one", project: "one" }),
      s({ id: "claude:b", cwd: "/p/one", project: "one" }),
      s({ id: "opencode:c", cwd: "/p/two", project: "two" }),
    ],
    { nowS: NOW },
  );
  const groups = nodes.filter((n) => n.type === "projectGroup");
  expect(groups).toHaveLength(2);
  expect(groups.find((g) => g.id === "group:/p/one")?.data).toMatchObject({
    project: "one",
    cwd: "/p/one",
    count: 2,
  });
});

test("clusters are collapsed by default — header only, no child chips", () => {
  const input = [s({ id: "claude:a", cwd: "/p/one" }), s({ id: "claude:b", cwd: "/p/one" })];
  const { nodes } = buildOverview(input, { nowS: NOW });
  expect(nodes.filter((n) => n.type === "session")).toHaveLength(0);
  expect(nodes.find((n) => n.type === "projectGroup")?.data).toMatchObject({ collapsed: true });
});

test("an expanded cluster renders its chips; a collapsed one does not", () => {
  const input = [s({ id: "claude:a", cwd: "/p/one" }), s({ id: "claude:b", cwd: "/p/two" })];
  const { nodes } = buildOverview(input, { nowS: NOW, expanded: new Set(["/p/one"]) });
  const chips = nodes.filter((n) => n.type === "session").map((n) => n.id);
  expect(chips).toEqual(["claude:a"]); // only the expanded cluster's chip
  const groups = Object.fromEntries(
    nodes.filter((n) => n.type === "projectGroup").map((n) => [n.id, n.data]),
  );
  expect(groups["group:/p/one"]).toMatchObject({ collapsed: false });
  expect(groups["group:/p/two"]).toMatchObject({ collapsed: true });
});

test("excluded cwds are dropped entirely (no group, no chips)", () => {
  const input = [s({ id: "claude:a", cwd: "/p/one" }), s({ id: "claude:b", cwd: "/p/secret" })];
  const { nodes } = buildOverview(input, {
    nowS: NOW,
    expanded: allExpanded(input),
    excluded: new Set(["/p/secret"]),
  });
  expect(nodes.some((n) => n.id === "group:/p/secret")).toBe(false);
  expect(nodes.some((n) => n.id === "claude:b")).toBe(false);
  expect(nodes.some((n) => n.id === "group:/p/one")).toBe(true);
});

test("hides archived by default, includes them when asked", () => {
  const input = [s({ id: "claude:a" }), s({ id: "claude:b", archived: true })];
  const exp = { expanded: allExpanded(input), nowS: NOW };
  const def = buildOverview(input, exp).nodes.filter((n) => n.type === "session");
  expect(def.map((n) => n.id)).toEqual(["claude:a"]);
  const all = buildOverview(input, { ...exp, includeArchived: true }).nodes.filter(
    (n) => n.type === "session",
  );
  expect(all.map((n) => n.id).sort()).toEqual(["claude:a", "claude:b"]);
});

test("active = last activity within the 15-min window; older is idle", () => {
  const input = [
    s({ id: "claude:fresh", last_mtime: NOW - 60 }),
    s({ id: "claude:stale", last_mtime: NOW - ACTIVE_WINDOW_S - 1 }),
  ];
  const { nodes } = buildOverview(input, { nowS: NOW, expanded: allExpanded(input) });
  const byId = Object.fromEntries(
    nodes.filter((n) => n.type === "session").map((n) => [n.id, n.data]),
  );
  expect(byId["claude:fresh"]).toMatchObject({ active: true });
  expect(byId["claude:stale"]).toMatchObject({ active: false });
});

test("each group node precedes its children (React Flow parent ordering)", () => {
  const input = [s({ id: "claude:a", cwd: "/p/one" }), s({ id: "claude:b", cwd: "/p/two" })];
  const { nodes } = buildOverview(input, { nowS: NOW, expanded: allExpanded(input) });
  for (const child of nodes.filter((n) => n.parentId)) {
    const gi = nodes.findIndex((n) => n.id === child.parentId);
    const ci = nodes.findIndex((n) => n.id === child.id);
    expect(gi).toBeGreaterThanOrEqual(0);
    expect(gi).toBeLessThan(ci);
    expect(child.extent).toBe("parent");
  }
});

test("chip order is deterministic: sticky first, then most-recent, then id", () => {
  const input = [
    s({ id: "claude:old", last_mtime: NOW - 1000 }),
    s({ id: "claude:new", last_mtime: NOW - 10 }),
    s({ id: "claude:pin", last_mtime: NOW - 5000, sticky: true }),
  ];
  const { nodes } = buildOverview(input, { nowS: NOW, expanded: allExpanded(input) });
  const order = nodes.filter((n) => n.type === "session").map((n) => n.id);
  expect(order).toEqual(["claude:pin", "claude:new", "claude:old"]);
});

// ---- hierarchy / edges (#148) -------------------------------------------------

test("links a nested project to its parent with an edge, child placed below", () => {
  const input = [
    s({ id: "claude:root", cwd: "/home/u/claude" }),
    s({ id: "claude:child", cwd: "/home/u/claude/demoapp.io" }),
  ];
  const { nodes, edges } = buildOverview(input, { nowS: NOW });
  expect(edges).toHaveLength(1);
  expect(edges[0]).toMatchObject({
    source: "group:/home/u/claude",
    target: "group:/home/u/claude/demoapp.io",
  });
  const y = Object.fromEntries(
    nodes.filter((n) => n.type === "projectGroup").map((n) => [n.id, n.position.y]),
  );
  expect(y["group:/home/u/claude/demoapp.io"]).toBeGreaterThan(y["group:/home/u/claude"]);
});

test("path matching is boundary-aware: /claude is NOT a parent of /claude-foo", () => {
  const input = [
    s({ id: "claude:a", cwd: "/home/u/claude" }),
    s({ id: "claude:b", cwd: "/home/u/claude-foo" }),
  ];
  const { edges } = buildOverview(input, { nowS: NOW });
  expect(edges).toHaveLength(0); // siblings, not parent/child
});

test("links to the NEAREST present ancestor, skipping absent intermediates", () => {
  // /a present, /a/b/c present, /a/b absent → c links to a (no synthetic /a/b node).
  const input = [
    s({ id: "claude:a", cwd: "/a" }),
    s({ id: "claude:c", cwd: "/a/b/c" }),
  ];
  const { nodes, edges } = buildOverview(input, { nowS: NOW });
  expect(nodes.some((n) => n.id === "group:/a/b")).toBe(false);
  expect(edges).toEqual([
    expect.objectContaining({ source: "group:/a", target: "group:/a/b/c" }),
  ]);
});

test("multiple roots, no edges between unrelated trees", () => {
  const input = [
    s({ id: "claude:a", cwd: "/a" }),
    s({ id: "claude:b", cwd: "/b" }),
    s({ id: "claude:ax", cwd: "/a/x" }),
  ];
  const { edges } = buildOverview(input, { nowS: NOW });
  expect(edges.map((e) => `${e.source}->${e.target}`)).toEqual(["group:/a->group:/a/x"]);
});

test("custom name is carried on the group node data (#148)", () => {
  const input = [s({ id: "claude:a", cwd: "/home/u/proj" })];
  const { nodes } = buildOverview(input, { nowS: NOW, names: { "/home/u/proj": "My Project" } });
  expect(nodes.find((n) => n.type === "projectGroup")?.data).toMatchObject({ name: "My Project" });
});

test("output is stable across calls (deterministic)", () => {
  const input = [
    s({ id: "claude:a", cwd: "/p/one", last_mtime: NOW - 5 }),
    s({ id: "opencode:b", cwd: "/p/two", last_mtime: NOW - 50 }),
  ];
  const opts = { nowS: NOW, expanded: allExpanded(input) };
  expect(buildOverview(input, opts)).toEqual(buildOverview(input, opts));
});

test("activeId marks the matching chip selected (#149 sidebar sync)", () => {
  const input = [
    s({ id: "claude:a", cwd: "/p/one" }),
    s({ id: "claude:b", cwd: "/p/one" }),
  ];
  const { nodes } = buildOverview(input, {
    nowS: NOW,
    expanded: new Set(["/p/one"]),
    activeId: "claude:a",
  });
  const byId = Object.fromEntries(
    nodes.filter((n) => n.type === "session").map((n) => [n.id, n.data]),
  );
  expect(byId["claude:a"]).toMatchObject({ selected: true });
  expect(byId["claude:b"]).toMatchObject({ selected: false });
});
