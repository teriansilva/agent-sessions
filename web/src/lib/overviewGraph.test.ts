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
  const one = groups.find((g) => g.id === "group:/p/one");
  expect(one?.data).toMatchObject({ project: "one", cwd: "/p/one", count: 2 });
});

test("hides archived by default, includes them when asked", () => {
  const input = [
    s({ id: "claude:a" }),
    s({ id: "claude:b", archived: true }),
  ];
  const def = buildOverview(input, { nowS: NOW }).nodes.filter((n) => n.type === "session");
  expect(def.map((n) => n.id)).toEqual(["claude:a"]);
  const all = buildOverview(input, { nowS: NOW, includeArchived: true }).nodes.filter(
    (n) => n.type === "session",
  );
  expect(all.map((n) => n.id).sort()).toEqual(["claude:a", "claude:b"]);
});

test("active = last activity within the 15-min window; older is idle", () => {
  const { nodes } = buildOverview(
    [
      s({ id: "claude:fresh", last_mtime: NOW - 60 }),
      s({ id: "claude:stale", last_mtime: NOW - ACTIVE_WINDOW_S - 1 }),
    ],
    { nowS: NOW },
  );
  const byId = Object.fromEntries(
    nodes.filter((n) => n.type === "session").map((n) => [n.id, n.data]),
  );
  expect(byId["claude:fresh"]).toMatchObject({ active: true });
  expect(byId["claude:stale"]).toMatchObject({ active: false });
});

test("each group node precedes its children (React Flow parent ordering)", () => {
  const { nodes } = buildOverview(
    [s({ id: "claude:a", cwd: "/p/one" }), s({ id: "claude:b", cwd: "/p/two" })],
    { nowS: NOW },
  );
  for (const child of nodes.filter((n) => n.parentId)) {
    const gi = nodes.findIndex((n) => n.id === child.parentId);
    const ci = nodes.findIndex((n) => n.id === child.id);
    expect(gi).toBeGreaterThanOrEqual(0);
    expect(gi).toBeLessThan(ci);
    expect(child.extent).toBe("parent");
  }
});

test("children carry parentId and sit inside the group via relative positions", () => {
  const { nodes } = buildOverview([s({ id: "claude:a", cwd: "/p/one" })], { nowS: NOW });
  const child = nodes.find((n) => n.type === "session");
  expect(child?.parentId).toBe("group:/p/one");
  expect(child?.position.x).toBeGreaterThan(0);
  expect(child?.position.y).toBeGreaterThan(0);
});

test("chip order is deterministic: sticky first, then most-recent, then id", () => {
  const { nodes } = buildOverview(
    [
      s({ id: "claude:old", last_mtime: NOW - 1000 }),
      s({ id: "claude:new", last_mtime: NOW - 10 }),
      s({ id: "claude:pin", last_mtime: NOW - 5000, sticky: true }),
    ],
    { nowS: NOW },
  );
  const order = nodes.filter((n) => n.type === "session").map((n) => n.id);
  expect(order).toEqual(["claude:pin", "claude:new", "claude:old"]);
});

test("clusters wrap to a new row instead of an unbounded horizontal strip", () => {
  // 12 single-session projects → must not all sit at y=0.
  const many = Array.from({ length: 12 }, (_, i) =>
    s({ id: `claude:${i}`, cwd: `/p/${i}`, project: `p${i}` }),
  );
  const groups = buildOverview(many, { nowS: NOW }).nodes.filter((n) => n.type === "projectGroup");
  const ys = new Set(groups.map((g) => g.position.y));
  expect(ys.size).toBeGreaterThan(1); // wrapped onto multiple rows
});

test("output is stable across calls (deterministic)", () => {
  const input = [
    s({ id: "claude:a", cwd: "/p/one", last_mtime: NOW - 5 }),
    s({ id: "opencode:b", cwd: "/p/two", last_mtime: NOW - 50 }),
  ];
  expect(buildOverview(input, { nowS: NOW })).toEqual(buildOverview(input, { nowS: NOW }));
});
