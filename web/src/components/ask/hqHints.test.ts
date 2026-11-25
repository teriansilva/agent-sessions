import { beforeEach, describe, expect, test } from "vitest";

import {
  cleanShown,
  HINTS,
  lastSet,
  loadShown,
  pickHints,
  recordShown,
  resetHintMemory,
  SET_SIZE,
  STORAGE_KEY,
  type Hint,
  type HintStore,
  type Shown,
} from "./hqHints";

beforeEach(() => resetHintMemory());

/** A store backed by a plain variable, with switchable failures. */
function memStore(initial: string | null = null) {
  const s = { value: initial, getThrows: false, setThrows: false, writes: 0 };
  const store: HintStore = {
    get: () => {
      if (s.getThrows) throw new Error("SecurityError");
      return s.value;
    },
    set: (v) => {
      if (s.setThrows) throw new Error("QuotaExceededError");
      s.writes += 1;
      s.value = v;
    },
  };
  return { s, store };
}

/** One "open": pick against what is stored (avoiding the last set), then record it. */
function open(
  store: HintStore | null,
  now: number,
  catalog: readonly Hint[] = HINTS,
): Hint[] {
  const shown = loadShown(store, catalog);
  const set = pickHints(catalog, shown, lastSet(shown));
  recordShown(store, set, now, catalog);
  return set;
}

describe("the catalog", () => {
  test("ids are unique and a set can always be filled", () => {
    const ids = HINTS.map((h) => h.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(HINTS.some((h) => h.kind === "manual")).toBe(true);
    expect(new Set(HINTS.map((h) => h.kind)).size).toBeGreaterThanOrEqual(
      SET_SIZE,
    );
    for (const h of HINTS) expect(Boolean(h.page)).toBe(h.kind === "manual");
  });
});

describe("pickHints", () => {
  test("a set is one manual hint first, then hints of other, distinct kinds", () => {
    const set = pickHints(HINTS, {});
    expect(set).toHaveLength(SET_SIZE);
    expect(set[0].kind).toBe("manual");
    expect(new Set(set.map((h) => h.kind)).size).toBe(SET_SIZE);
  });

  test("never-shown hints come before shown ones, then the least recently shown", () => {
    const manual = HINTS.filter((h) => h.kind === "manual");
    const shown: Shown = Object.fromEntries(
      manual.map((h, i) => [h.id, 100 + i]),
    );
    delete shown[manual[5].id];
    expect(pickHints(HINTS, shown)[0].id).toBe(manual[5].id);
    shown[manual[5].id] = 999;
    expect(pickHints(HINTS, shown)[0].id).toBe(manual[0].id); // oldest time wins
  });

  test("the manual slot cycles every manual hint before one repeats", () => {
    const { store } = memStore();
    const manual = HINTS.filter((h) => h.kind === "manual");
    const seen = Array.from(
      { length: manual.length },
      (_, i) => open(store, 1000 + i)[0].id,
    );
    expect(new Set(seen).size).toBe(manual.length);
    // The next open starts the cycle again with the hint shown longest ago.
    expect(open(store, 5000)[0].id).toBe(seen[0]);
  });

  test("a non-manual hint is never shown while an unseen one of an eligible kind exists", () => {
    const { store } = memStore();
    const rest = HINTS.filter((h) => h.kind !== "manual");
    const seen: string[] = [];
    for (let i = 0; seen.length + 2 <= rest.length; i++) {
      const set = open(store, 1000 + i).slice(1);
      for (const h of set) expect(seen).not.toContain(h.id);
      seen.push(...set.map((h) => h.id));
    }
    // 14 non-manual hints last 7 opens at two per open.
    expect(seen).toHaveLength(rest.length);
  });

  test("two opens in a row never share a hint, over many opens", () => {
    const { store } = memStore();
    let prev = new Set<string>();
    for (let i = 0; i < 40; i++) {
      const ids = open(store, 1000 + i).map((h) => h.id);
      for (const id of ids) expect(prev.has(id)).toBe(false);
      prev = new Set(ids);
    }
  });

  test("Other ideas avoids the set on screen", () => {
    const first = pickHints(HINTS, {});
    const next = pickHints(HINTS, {}, new Set(first.map((h) => h.id)));
    expect(
      next.map((h) => h.id).some((id) => first.some((h) => h.id === id)),
    ).toBe(false);
  });

  test("a tiny catalog still fills what it can, never throws", () => {
    const tiny: Hint[] = [HINTS[0], HINTS[8]];
    expect(pickHints(tiny, {}).map((h) => h.id)).toEqual([
      HINTS[0].id,
      HINTS[8].id,
    ]);
    expect(pickHints([], {})).toEqual([]);
  });
});

describe("storage is best effort", () => {
  test("only catalog ids with a finite, non-negative time are kept", () => {
    const raw = {
      [HINTS[0].id]: 5,
      [HINTS[1].id]: -1,
      [HINTS[2].id]: Number.NaN,
      [HINTS[3].id]: "7",
      [HINTS[4].id]: Infinity,
      gone: 9,
    };
    expect(cleanShown(raw)).toEqual({ [HINTS[0].id]: 5 });
    expect(cleanShown(null)).toEqual({});
    expect(cleanShown([1, 2])).toEqual({});
  });

  test.each([
    ["not json", "{nope"],
    ["wrong shape", JSON.stringify({ shown: [1, 2, 3] })],
    ["a bare string", JSON.stringify("x")],
  ])("an unparsable stored value (%s) reads as empty", (_why, value) => {
    const { store } = memStore(value);
    expect(loadShown(store)).toEqual({});
  });

  test("a read that throws falls back to memory, and rotation continues", () => {
    const { s, store } = memStore();
    s.getThrows = true;
    const a = open(store, 1000);
    const b = open(store, 2000);
    expect(a.map((h) => h.id)).not.toEqual(b.map((h) => h.id));
  });

  test("a write that throws keeps the rotation in memory: the next open and Other ideas move on", () => {
    const { s, store } = memStore();
    s.setThrows = true;
    const a = open(store, 1000);
    expect(s.writes).toBe(0);
    const b = open(store, 2000);
    for (const h of b) expect(a.map((x) => x.id)).not.toContain(h.id);
    const shown = loadShown(store);
    const other = pickHints(HINTS, shown, new Set(b.map((h) => h.id)));
    for (const h of other) expect(b.map((x) => x.id)).not.toContain(h.id);
  });

  test("no storage at all still rotates", () => {
    const a = open(null, 1000);
    const b = open(null, 2000);
    expect(a[0].id).not.toBe(b[0].id);
  });

  test("a write prunes ids the catalog no longer has", () => {
    const { s, store } = memStore(
      JSON.stringify({ shown: { retired: 5, [HINTS[0].id]: 6 } }),
    );
    recordShown(store, [HINTS[1]], 10);
    expect(JSON.parse(s.value!).shown).toEqual({
      [HINTS[0].id]: 6,
      [HINTS[1].id]: 10,
    });
  });

  test("the key is versioned", () => {
    expect(STORAGE_KEY).toBe("battlelab.hqHints.v1");
  });
});
