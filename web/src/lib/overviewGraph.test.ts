import { expect, test } from "vitest";
import type { Session } from "../types/api";
import {
  ACTIVE_WINDOW_S,
  buildOverview,
  expandableKeys,
  type ProjectGroupData,
} from "./overviewGraph";

const NOW = 1_700_000_000;

function s(over: Partial<Session> & { id: string }): Session {
  return {
    engine: "claude",
    uuid: over.id,
    short_uuid: over.id.slice(0, 6),
    cwd: "/home/u/proj",
    project: { kind: "folder" as const, id: "/home/u/proj", name: "proj" },
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
      s({ id: "claude:a", cwd: "/p/one", project: { kind: "folder" as const, id: "/p/one", name: "one" } }),
      s({ id: "claude:b", cwd: "/p/one", project: { kind: "folder" as const, id: "/p/one", name: "one" } }),
      s({ id: "opencode:c", cwd: "/p/two", project: { kind: "folder" as const, id: "/p/two", name: "two" } }),
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

test("cwd visibility prefs never drop project-resolved sessions (#361)", () => {
  const inProject = s({
    id: "claude:p",
    cwd: "/p/hidden",
    project: { kind: "project" as const, id: "p-1", name: "Side" },
  });
  const inFolder = s({
    id: "claude:f",
    cwd: "/p/hidden",
    project: { kind: "folder" as const, id: "/p/hidden", name: "/p/hidden" },
  });
  const { nodes } = buildOverview([inProject, inFolder], {
    nowS: NOW,
    excluded: new Set(["/p/hidden"]),
  });
  const group = nodes.find((n) => n.id === "group:project:p-1");
  // the folder-grouped session is dropped by the pref; the project member survives
  expect(group?.data).toMatchObject({ project: "Side", kind: "project", count: 1 });
  expect(nodes.some((n) => n.id === "group:/p/hidden")).toBe(false);
});

test("Expand all covers project-resolved clusters whose cwd is hidden (#361)", () => {
  const inProject = s({
    id: "claude:p",
    cwd: "/p/hidden",
    project: { kind: "project" as const, id: "p-1", name: "Side" },
  });
  const plain = s({ id: "claude:f", cwd: "/p/dropped" });
  expect(expandableKeys([inProject, plain], new Set(["/p/hidden", "/p/dropped"]))).toEqual([
    "project:p-1",
  ]);
});

// ---- entity clustering (#361 Phase 4) -----------------------------------------

const ref = (over: Partial<Session["project"]> = {}): Session["project"] => ({
  kind: "project" as const,
  id: "p-1",
  name: "Side",
  ...over,
});

test("an entity spanning two cwds yields ONE merged group, keyed project:<id>", () => {
  const input = [
    s({ id: "claude:a", cwd: "/p/app", project: ref() }),
    s({ id: "claude:b", cwd: "/p/lib", project: ref() }),
    s({ id: "claude:c", cwd: "/p/lib", project: ref() }),
  ];
  const { nodes } = buildOverview(input, { nowS: NOW });
  const groups = nodes.filter((n) => n.type === "projectGroup");
  expect(groups).toHaveLength(1);
  expect(groups[0].id).toBe("group:project:p-1");
  expect(groups[0].data).toMatchObject({
    project: "Side",
    kind: "project",
    groupKey: "project:p-1",
    count: 3,
    cwdCount: 2,
    cwd: "/p/app", // representative: first sorted member cwd
  });
});

test("a folderless explicit assignment lands in its entity group (#361)", () => {
  const input = [
    s({ id: "claude:a", cwd: "/p/adopted", project: ref() }),
    // explicit per-session assignment from an unrelated cwd → same cluster
    s({ id: "claude:x", cwd: "/elsewhere/scratch", project: ref() }),
  ];
  const { nodes } = buildOverview(input, { nowS: NOW });
  const groups = nodes.filter((n) => n.type === "projectGroup");
  expect(groups).toHaveLength(1);
  expect(groups[0].data).toMatchObject({ groupKey: "project:p-1", count: 2, cwdCount: 2 });
  expect(nodes.some((n) => n.id === "group:/elsewhere/scratch")).toBe(false);
});

test("folder fallback grouping/ids are unchanged next to entity groups", () => {
  const input = [
    s({ id: "claude:a", cwd: "/p/one", project: { kind: "folder" as const, id: "/p/one", name: "one" } }),
    s({ id: "claude:b", cwd: "/p/two", project: ref() }),
  ];
  const { nodes } = buildOverview(input, { nowS: NOW });
  expect(nodes.find((n) => n.id === "group:/p/one")?.data).toMatchObject({
    kind: "folder",
    groupKey: "/p/one",
    cwd: "/p/one",
    cwdCount: 1,
  });
  expect(nodes.some((n) => n.id === "group:project:p-1")).toBe(true);
});

test("nesting edges link FOLDER groups only — entity groups are roots (#361 Phase 4)", () => {
  const input = [
    s({ id: "claude:r", cwd: "/a", project: { kind: "folder" as const, id: "/a", name: "a" } }),
    s({ id: "claude:c", cwd: "/a/b", project: { kind: "folder" as const, id: "/a/b", name: "b" } }),
    // an entity member nested under /a must NOT get a hierarchy edge
    s({ id: "claude:p", cwd: "/a/proj", project: ref() }),
  ];
  const { nodes, edges } = buildOverview(input, { nowS: NOW });
  expect(edges.map((e) => `${e.source}->${e.target}`)).toEqual(["group:/a->group:/a/b"]);
  // the entity group sits in the root row, alongside /a
  const y = Object.fromEntries(
    nodes.filter((n) => n.type === "projectGroup").map((n) => [n.id, n.position.y]),
  );
  expect(y["group:project:p-1"]).toBe(y["group:/a"]);
});

test("entity clusters expand by their project:<id> toggle key", () => {
  const input = [
    s({ id: "claude:a", cwd: "/p/app", project: ref() }),
    s({ id: "claude:b", cwd: "/p/lib", project: ref() }),
  ];
  const collapsed = buildOverview(input, { nowS: NOW, expanded: new Set(["/p/app"]) });
  expect(collapsed.nodes.filter((n) => n.type === "session")).toHaveLength(0); // cwd key ≠ toggle key
  const open = buildOverview(input, { nowS: NOW, expanded: new Set(["project:p-1"]) });
  expect(open.nodes.filter((n) => n.type === "session").map((n) => n.parentId)).toEqual([
    "group:project:p-1",
    "group:project:p-1",
  ]);
});

test("entity color passes through to the group node data", () => {
  const input = [s({ id: "claude:a", cwd: "/p/app", project: ref({ color: "#5fd7ff" }) })];
  const { nodes } = buildOverview(input, { nowS: NOW });
  expect(nodes.find((n) => n.type === "projectGroup")?.data).toMatchObject({ color: "#5fd7ff" });
});

// ---- groupBy modes (#424 Phase 2) ---------------------------------------------

const kindOf = (data: unknown) => (data as ProjectGroupData).kind;
const colorOf = (data: unknown) => (data as ProjectGroupData).color;

// One fixture exercising all three modes: two engines, one entity spanning two cwds, plus a
// plain folder-fallback session.
const mixed = (): Session[] => [
  s({ id: "claude:a", engine: "claude", cwd: "/p/app", project: ref() }),
  s({ id: "opencode:b", engine: "opencode", cwd: "/p/lib", project: ref() }),
  s({
    id: "claude:c",
    engine: "claude",
    cwd: "/p/two",
    project: { kind: "folder" as const, id: "/p/two", name: "two" },
  }),
];

test("project mode (default): clusters by entity, folder fallback for the rest", () => {
  const { nodes } = buildOverview(mixed(), { nowS: NOW, groupBy: "project" });
  const groups = nodes.filter((n) => n.type === "projectGroup").map((n) => n.id).sort();
  expect(groups).toEqual(["group:/p/two", "group:project:p-1"]);
  expect(nodes.find((n) => n.id === "group:project:p-1")?.data).toMatchObject({
    kind: "project",
    count: 2,
    cwdCount: 2,
  });
});

test("folder mode: pure cwd tree — an entity splits back out by launch folder", () => {
  const { nodes } = buildOverview(mixed(), { nowS: NOW, groupBy: "folder" });
  const groups = nodes.filter((n) => n.type === "projectGroup");
  expect(groups.map((n) => n.id).sort()).toEqual(["group:/p/app", "group:/p/lib", "group:/p/two"]);
  expect(groups.map((n) => kindOf(n.data))).toEqual(["folder", "folder", "folder"]);
  expect(nodes.some((n) => n.id === "group:project:p-1")).toBe(false);
});

test("agent mode: one cluster per engine, labelled + colored by engine", () => {
  const { nodes } = buildOverview(mixed(), { nowS: NOW, groupBy: "agent" });
  const groups = Object.fromEntries(
    nodes.filter((n) => n.type === "projectGroup").map((n) => [n.id, n.data]),
  );
  expect(Object.keys(groups).sort()).toEqual(["group:agent:claude", "group:agent:opencode"]);
  expect(groups["group:agent:claude"]).toMatchObject({ kind: "agent", project: "claude", count: 2 });
  expect(groups["group:agent:opencode"]).toMatchObject({ kind: "agent", project: "opencode", count: 1 });
  expect(colorOf(groups["group:agent:opencode"])).toBe("#4fd1c5"); // engine accent flows to the cue
});

test("agent mode has no hierarchy edges — every engine group is a root (#424)", () => {
  const input = [
    s({ id: "claude:a", engine: "claude", cwd: "/a" }),
    s({ id: "claude:b", engine: "claude", cwd: "/a/b" }), // nested cwd, but agent mode ignores folders
  ];
  const { edges, nodes } = buildOverview(input, { nowS: NOW, groupBy: "agent" });
  expect(edges).toHaveLength(0);
  expect(nodes.filter((n) => n.type === "projectGroup")).toHaveLength(1); // both → one claude group
});

test("agent clusters expand by their agent:<engine> toggle key", () => {
  const open = buildOverview(mixed(), {
    nowS: NOW,
    groupBy: "agent",
    expanded: new Set(["agent:claude"]),
  });
  const chips = open.nodes.filter((n) => n.type === "session");
  expect(chips.map((n) => n.id).sort()).toEqual(["claude:a", "claude:c"]);
  expect(chips.every((n) => n.parentId === "group:agent:claude")).toBe(true);
});

test("folder/agent mode drops a hidden cwd's sessions even when entity-resolved (#424)", () => {
  const input = [s({ id: "claude:p", cwd: "/p/hidden", project: ref() })];
  const excluded = new Set(["/p/hidden"]);
  // project mode keeps the entity member (server parity); folder + agent drop it (cwd hidden)
  expect(
    buildOverview(input, { nowS: NOW, groupBy: "project", excluded }).nodes.some(
      (n) => n.id === "group:project:p-1",
    ),
  ).toBe(true);
  for (const groupBy of ["folder", "agent"] as const) {
    const { nodes } = buildOverview(input, { nowS: NOW, groupBy, excluded });
    expect(nodes.filter((n) => n.type === "projectGroup")).toHaveLength(0);
  }
});

test("expandableKeys returns mode-appropriate toggle keys (#424)", () => {
  const input = mixed();
  expect(expandableKeys(input, new Set(), "project").sort()).toEqual(["/p/two", "project:p-1"]);
  expect(expandableKeys(input, new Set(), "folder").sort()).toEqual([
    "/p/app",
    "/p/lib",
    "/p/two",
  ]);
  expect(expandableKeys(input, new Set(), "agent").sort()).toEqual([
    "agent:claude",
    "agent:opencode",
  ]);
});

test("draggableSessions makes chips draggable and drops the parent clamp (#424 Phase 5)", () => {
  const input = [s({ id: "claude:a", cwd: "/p/one" })];
  const expanded = new Set(["/p/one"]);
  const off = buildOverview(input, { nowS: NOW, expanded }).nodes.find((n) => n.type === "session");
  expect(off?.draggable).toBe(false);
  expect(off?.extent).toBe("parent");
  const on = buildOverview(input, { nowS: NOW, expanded, draggableSessions: true }).nodes.find(
    (n) => n.type === "session",
  );
  expect(on?.draggable).toBe(true);
  // The parent clamp is gone so the chip can be dragged out onto another cluster…
  expect(on?.extent).toBeUndefined();
  // …but it stays parented for layout/containment.
  expect(on?.parentId).toBe("group:/p/one");
});
