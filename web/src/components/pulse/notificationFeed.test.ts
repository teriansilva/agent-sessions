/** #2146: the join of the bell's rows and HQ's dispatches, by event tag. */
import { expect, test } from "vitest";

import type {
  HqDispatch,
  HqDispatchMember,
  PulseNotification,
} from "../../types/api";
import {
  bellGroups,
  buildFeed,
  bundlesToLookUp,
  classOfRow,
  countFeed,
  filterFeed,
  MAX_BUNDLE_LOOKUPS,
  type FeedItem,
} from "./notificationFeed";
import { targetPath } from "./notificationLinks";

const SID = "claude:11111111-1111-4111-8111-111111111111";
const NY1 = `needs-you:${SID}:1`;
const NY2 = `needs-you:${SID}:2`;
const AUTO = "automation:nightly:r1";
const ESC = `escalation:msn_${"a".repeat(32)}:${"b".repeat(12)}:1`;

function row(
  id: string,
  ts: number,
  extra: Partial<PulseNotification> = {},
): PulseNotification {
  return {
    id,
    ts,
    read: false,
    title: `row ${id}`,
    project: "api",
    reason: "",
    session_id: SID,
    engine: "claude",
    action_id: "",
    ...extra,
  };
}

const inBundle = (bid: string, tag: string, bundled = 3) => ({
  dispatch: {
    id: bid,
    line: `line of ${bid}`,
    by: "Gen. Okafor",
    bundled,
    tag,
  },
});

function member(tag: string, ts: number, cls = "needs you"): HqDispatchMember {
  return {
    class: cls,
    title: `member ${tag}`,
    reason: "",
    link: "/s/claude/x",
    ts,
    tag,
  };
}

function dispatch(
  id: string,
  at: number,
  members: HqDispatchMember[],
): HqDispatch {
  return {
    id,
    at,
    opened_at: at - 60,
    line: `line of ${id}`,
    contact: "General Amara Okafor",
    by: "Gen. Okafor",
    members,
  };
}

const D1 = "d_0000000000000001";
const D2 = "d_0000000000000002";

const events = (i: FeedItem) => (i.kind === "bundle" ? i.events : [i.event]);
const keys = (feed: FeedItem[]) => feed.map((i) => events(i).map((e) => e.key));

test("a partial bundle: the live rows ARE their members, the rest come from the snapshot", () => {
  const d = dispatch(D1, 200, [
    member(NY1, 100),
    member(AUTO, 110, "automation failed"),
    member(ESC, 120, "mission escalation"),
  ]);
  // The escalation's row was dismissed; the other two are live.
  const items = [
    row("a", 110, { session_id: "", action_id: AUTO, ...inBundle(D1, AUTO) }),
    row("n", 100, { needs_you: true, ...inBundle(D1, NY1) }),
  ];
  const feed = buildFeed(items, [d]);
  expect(feed).toHaveLength(1);
  const [b] = feed;
  expect(b.kind).toBe("bundle");
  if (b.kind !== "bundle") return;
  expect(b.head).toMatchObject({
    line: `line of ${D1}`,
    contact: "General Amara Okafor",
    at: 200,
    count: 3,
  });
  // Newest first inside the bundle; each event exactly once.
  expect(b.events.map((e) => [e.kind, e.key])).toEqual([
    ["gone", `m:${D1}:${ESC}`],
    ["live", "n:a"],
    ["live", "n:n"],
  ]);
  expect(b.ts).toBe(120);
});

test("a dismissed member stays reachable from the snapshot; a fully dismissed bundle still shows", () => {
  const feed = buildFeed([], [dispatch(D1, 200, [member(NY1, 100)])]);
  expect(feed).toHaveLength(1);
  expect(events(feed[0]).map((e) => e.kind)).toEqual(["gone"]);
});

test("two events for the same session are two tags, so two rows", () => {
  const d = dispatch(D1, 300, [member(NY1, 100), member(NY2, 200)]);
  const items = [
    row("two", 200, inBundle(D1, NY2, 2)),
    row("one", 100, inBundle(D1, NY1, 2)),
  ];
  const [b] = buildFeed(items, [d]);
  expect(events(b).map((e) => e.key)).toEqual(["n:two", "n:one"]);
});

test("a row whose tag the bundle doesn't list is still shown, once, in the server's bundle", () => {
  const d = dispatch(D1, 300, [member(NY1, 100)]);
  const items = [row("x", 150, inBundle(D1, NY2, 1))];
  const [b] = buildFeed(items, [d]);
  expect(events(b).map((e) => e.key)).toEqual(["n:x", `m:${D1}:${NY1}`]);
});

test("a live row whose bundle wasn't read (outside the newest 50) heads its group from its own line", () => {
  const items = [row("old", 50, inBundle(D2, NY1, 2)), row("solo", 60)];
  const feed = buildFeed(items, []);
  expect(feed.map((i) => i.kind)).toEqual(["solo", "bundle"]);
  const b = feed[1];
  if (b.kind !== "bundle") throw new Error("bundle");
  expect(b.head).toEqual({
    line: `line of ${D2}`,
    by: "Gen. Okafor",
    contact: null,
    at: null,
    count: 2,
  });
  expect(bundlesToLookUp(items, new Set())).toEqual([D2]);
  // Once read, the snapshot fills in the member the bell no longer holds.
  const read = buildFeed(items, [
    dispatch(D2, 70, [member(NY1, 50), member(AUTO, 40, "automation failed")]),
  ]);
  expect(keys(read)).toEqual([["n:solo"], ["n:old", `m:${D2}:${AUTO}`]]);
});

test("bundle lookups are deduplicated, skip what is known and are bounded", () => {
  const items = Array.from({ length: 30 }, (_, i) =>
    row(
      `r${i}`,
      1000 - i,
      inBundle(`d_${(i % 15).toString(16).padStart(16, "0")}`, NY1),
    ),
  );
  const ids = bundlesToLookUp(items, new Set(["d_0000000000000000"]));
  expect(ids).toHaveLength(MAX_BUNDLE_LOOKUPS);
  expect(new Set(ids).size).toBe(ids.length);
  expect(ids).not.toContain("d_0000000000000000");
});

test("the first copy of a dispatch wins (the list before a single read)", () => {
  const listed = dispatch(D1, 200, [member(NY1, 100)]);
  const stale = { ...dispatch(D1, 200, []), line: "stale" };
  const [b] = buildFeed([], [listed, stale]);
  if (b.kind !== "bundle") throw new Error("bundle");
  expect(b.head.line).toBe(`line of ${D1}`);
});

test("bundles sort by their newest event among the solo rows", () => {
  const items = [row("s1", 500), row("s0", 100)];
  const feed = buildFeed(items, [dispatch(D1, 999, [member(NY1, 300)])]);
  expect(feed.map((i) => (i.kind === "bundle" ? i.id : i.event.key))).toEqual([
    "n:s1",
    D1,
    "n:s0",
  ]);
});

test("snapshot-only members: under All and their class, never unread, never a bulk-action count", () => {
  const d = dispatch(D1, 300, [
    member(NY1, 100),
    member(AUTO, 110, "automation failed"),
  ]);
  const items = [
    row("live", 110, {
      session_id: "",
      action_id: AUTO,
      ...inBundle(D1, AUTO, 2),
    }),
    row("solo", 50, { read: true, escalation: true }),
  ];
  const feed = buildFeed(items, [d]);
  expect(countFeed(feed)).toEqual({
    all: 3,
    unread: 1,
    needs: 1,
    escalation: 1,
    automation: 1,
  });
  expect(keys(filterFeed(feed, "unread"))).toEqual([["n:live"]]);
  expect(keys(filterFeed(feed, "needs"))).toEqual([[`m:${D1}:${NY1}`]]);
  expect(keys(filterFeed(feed, "escalation"))).toEqual([["n:solo"]]);
  // What Mark all read / Clear all act on is the bell's own rows, never the feed's All.
  expect(items.length).toBe(2);
});

test("a row's class comes from its tag, else its producer's stored flags", () => {
  expect(classOfRow(row("a", 1, inBundle(D1, ESC)))).toBe("escalation");
  expect(
    classOfRow(row("b", 1, { session_id: "", action_id: "automation:x:1" })),
  ).toBe("automation");
  expect(classOfRow(row("c", 1, { needs_you: true }))).toBe("needs");
  expect(classOfRow(row("d", 1, { escalation: true }))).toBe("escalation");
  expect(classOfRow(row("e", 1))).toBe("other");
  // A "Tell HQ" message (#2221) files with the automations, by its tag or by its action id.
  const MSG = `automsg:nightly:${"0".repeat(32)}`;
  expect(classOfRow(row("f", 1, inBundle(D1, MSG)))).toBe("automation");
  expect(classOfRow(row("g", 1, { session_id: "", action_id: MSG }))).toBe("automation");
  // …and opens that automation, as the server's push link does.
  expect(targetPath(row("h", 1, { session_id: "", action_id: MSG }))).toBe(
    "/mission/automations/nightly",
  );
});

test("the bell keeps its order but sits a bundle's rows together at its newest", () => {
  const items = [
    row("a", 5, inBundle(D1, NY1)),
    row("b", 4),
    row("c", 3, inBundle(D1, NY2)),
    row("d", 2, inBundle(D2, AUTO)),
  ];
  expect(
    bellGroups(items).map((g) => [g.bid, g.rows.map((r) => r.id)]),
  ).toEqual([
    [D1, ["a", "c"]],
    [null, ["b"]],
    [D2, ["d"]],
  ]);
});
