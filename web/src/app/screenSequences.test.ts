/** The guarantee, tested by enumeration (#2102, review round 2 of PR #2109):
 *
 *    For ANY sequence of map directives delivered before React commits, what the lines report
 *    equals what the real workspace reducer ends up with.
 *
 *  Three rounds of review each found one more ORDER of two or three map commands that the
 *  executor got wrong, because it decided from a rendered snapshot that lags its own dispatches.
 *  Instead of a fourth named case, this runs every ordered sequence of length 2 and 3 (and a
 *  deterministic sample of length 4) over the map's seven intents, from eight starting
 *  workspaces, two ways:
 *
 *    - **batch**: all directives handed to the executor, none rendered in between, then the
 *      map's effects;
 *    - **one at a time**: each directive followed by a render and the map's effects.
 *
 *  and asserts (a) both end in the SAME workspace (windows, parked state, geometry, stacking,
 *  the route on screen) with the SAME outcome for every directive, and (b) every outcome is true
 *  of that final workspace, with nothing left pending. Real executor, real reducer
 *  (`test/screenHarness.ts`). */
import { describe, expect, it } from "vitest";
import { MAP_PATH } from "../lib/routes";
import { sessionOf, type ScreenDirective, type ScreenOutcome } from "./screenDirective";
import { harness, id, key, stored, type Harness, type HarnessOptions } from "../test/screenHarness";

const A = key("a");
const B = key("b");
const openMap = (k: string) => ({ op: "open", target: { type: "session", id: k }, where: "map" });
const INTENTS: Record<string, object> = {
  "open A": openMap(A),
  "open B": openMap(B),
  "focus A": { op: "map_focus", key: A },
  "minimize A": { op: "map_minimize", key: A },
  "close A": { op: "map_close", key: A },
  "close all": { op: "map_close_all" },
  arrange: { op: "map_arrange" },
};
const NAMES = Object.keys(INTENTS);

const STARTS: Record<string, HarnessOptions> = {
  "an empty map": { path: MAP_PATH },
  "A open": { path: MAP_PATH, layout: [stored("a")] },
  "A minimised": { path: MAP_PATH, layout: [stored("a", true)] },
  "a full map without A": { path: MAP_PATH, cap: 2, layout: [stored("x"), stored("y")] },
  "a full map with A": { path: MAP_PATH, cap: 2, layout: [stored("a"), stored("x")] },
  "a saved layout with A, the map not mounted": { layout: [stored("a"), stored("x")] },
  "every window minimised": { path: MAP_PATH, layout: [stored("a", true), stored("x", true)] },
  "every saved window minimised, the map not mounted": {
    layout: [stored("a", true), stored("x", true)],
  },
};

const typed = { source: "typed" as const, turnId: "t1" };

function start(opts: HarnessOptions): Harness {
  const h = harness(opts);
  h.canvas(); // a mounted map restores its layout; elsewhere this only renders
  return h;
}

/** Everything about where the workspace ended up that a Commander could see. */
function picture(h: Harness, n: number) {
  const s = h.state();
  return {
    path: h.io.path,
    windows: s.windows
      .map((w) => ({ key: w.actionKey, minimized: Boolean(w.minimized), rect: w.rect, z: w.z }))
      .sort((a, b) => a.key.localeCompare(b.key)),
    saved: s.hydrated ? [] : s.restorable.map((w) => [w.key, Boolean(w.minimized)]),
    queue: s.queue.length,
    outcomes: Array.from({ length: n }, (_, i) => h.outcome(i + 1)),
  };
}

async function run(opts: HarnessOptions, names: string[], oneAtATime: boolean) {
  const h = start(opts);
  for (const [i, name] of names.entries()) {
    h.ex.execute({ id: id(i + 1), ...INTENTS[name] }, typed);
    if (oneAtATime) await h.flush();
  }
  await h.flush();
  return h;
}

/** (b): is each reported outcome true of the workspace as it ended up? */
function untruths(h: Harness, names: string[]): string[] {
  const wrong: string[] = [];
  const final = new Map(h.windows());
  names.forEach((name, i) => {
    const d = { id: id(i + 1), ...INTENTS[name] } as ScreenDirective;
    const o = h.outcome(i + 1) as ScreenOutcome | undefined;
    const k = sessionOf(d);
    const say = (what: string) => wrong.push(`#${i + 1} ${name}: ${what}`);
    if (!o) return say("has no outcome");
    if (o.state === "pending") return say("is still pending");
    if (o.state === "shown" && o.where === "map") {
      if (!final.has(k!)) say("says shown, but the session has no window");
      else if (final.get(k!)) say("says shown, but the window is minimised");
    }
    if (o.state !== "done") return;
    if (d.op === "map_minimize" && final.get(k!) !== true) say("says minimised, but it is not");
    if (d.op === "map_close" && final.has(k!)) say("says closed, but the window is there");
    if (d.op === "map_close_all") {
      // True of every window it closed: whatever is there now was asked for after it.
      const later = new Set(
        names.slice(i + 1).map((n) => sessionOf({ id: "", ...INTENTS[n] } as ScreenDirective)),
      );
      for (const w of final.keys())
        if (!later.has(w)) say(`says all closed, but ${w} is still there`);
    }
  });
  return wrong;
}

async function check(startName: string, names: string[]) {
  const batch = await run(STARTS[startName], names, false);
  const serial = await run(STARTS[startName], names, true);
  const label = `${startName} · ${names.join(" → ")}`;
  expect(picture(batch, names.length), label).toEqual(picture(serial, names.length));
  expect(untruths(batch, names), label).toEqual([]);
  expect(batch.state().queue, label).toEqual([]);
}

function* sequences(length: number): Generator<string[]> {
  if (length === 0) return void (yield []);
  for (const rest of sequences(length - 1)) for (const name of NAMES) yield [...rest, name];
}

/** A fixed pseudo-random sample, so a failure names the same sequence on every run. */
function sample(length: number, count: number): string[][] {
  let seed = 20_102;
  const next = () => (seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648);
  return Array.from({ length: count }, () =>
    Array.from({ length }, () => NAMES[next() % NAMES.length]),
  );
}

describe.each(Object.keys(STARTS))("from %s", (startName) => {
  it("every sequence of two directives ends where one-at-a-time ends, and says so", async () => {
    for (const names of sequences(2)) await check(startName, names);
  });

  it("every sequence of three", async () => {
    for (const names of sequences(3)) await check(startName, names);
  }, 60_000);

  it("a sample of sequences of four", async () => {
    for (const names of sample(4, 150)) await check(startName, names);
  }, 60_000);
});

it("the enumeration covers what it claims", () => {
  expect([...sequences(2)]).toHaveLength(49);
  expect([...sequences(3)]).toHaveLength(343);
  expect(new Set(sample(4, 150).map((s) => s.join())).size).toBeGreaterThan(140);
});
