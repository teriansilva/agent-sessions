import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  MAX_BYTES,
  MAX_ENTRIES,
  SENT_HISTORY_KEY,
  appendSent,
  clearSent,
  confirmSent,
  readSent,
} from "./sentHistory";

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

const add = (text: string, session: string | null = "claude:s1") =>
  appendSent({ text, attachments: [], session });

test("records newest first and confirms by id", () => {
  const a = add("first");
  const b = add("second");
  expect(readSent().map((e) => e.text)).toEqual(["second", "first"]);
  expect(readSent().every((e) => !e.confirmed)).toBe(true); // recorded before delivery

  confirmSent(b!);
  const [second, first] = readSent();
  expect(second.confirmed).toBe(true);
  expect(first.confirmed).toBe(false); // only the named entry flips
  expect(a).not.toBe(b);
});

test("confirming an unknown / evicted id is a no-op", () => {
  add("only");
  confirmSent("does-not-exist");
  expect(readSent()).toHaveLength(1);
  expect(readSent()[0].confirmed).toBe(false);
});

test(`keeps at most ${MAX_ENTRIES}, evicting the oldest`, () => {
  for (let i = 0; i < MAX_ENTRIES + 5; i++) add(`msg-${i}`);
  const entries = readSent();
  expect(entries).toHaveLength(MAX_ENTRIES);
  expect(entries[0].text).toBe(`msg-${MAX_ENTRIES + 4}`); // newest
  expect(entries.at(-1)!.text).toBe("msg-5"); // oldest survivor
});

test("a giant paste evicts older entries rather than blowing the byte cap", () => {
  add("small one");
  add("small two");
  add("x".repeat(MAX_BYTES)); // alone exceeds the budget
  const entries = readSent();
  expect(entries).toHaveLength(1); // the newest is always kept — it's what you came to recover
  expect(entries[0].text).toHaveLength(MAX_BYTES);
});

test("round-trips attachments and the session tag (null for a fresh launch)", () => {
  appendSent({ text: "look", attachments: ["/tmp/a.png", "/tmp/b.png"], session: null });
  const [e] = readSent();
  expect(e.attachments).toEqual(["/tmp/a.png", "/tmp/b.png"]);
  expect(e.session).toBeNull(); // #616's fresh-session loss must be recordable
});

test("text is stored untrimmed so Restore round-trips exactly", () => {
  add("  padded\n\n");
  expect(readSent()[0].text).toBe("  padded\n\n");
});

test("clearSent drops the ring (sign-out)", () => {
  add("secret prompt");
  clearSent();
  expect(readSent()).toEqual([]);
  expect(localStorage.getItem(SENT_HISTORY_KEY)).toBeNull();
});

test("a corrupt or foreign ring reads as empty, never throws", () => {
  localStorage.setItem(SENT_HISTORY_KEY, "{not json");
  expect(readSent()).toEqual([]);
  localStorage.setItem(SENT_HISTORY_KEY, JSON.stringify({ nope: 1 }));
  expect(readSent()).toEqual([]);
  localStorage.setItem(SENT_HISTORY_KEY, JSON.stringify([{ id: 1 }, null, "x"]));
  expect(readSent()).toEqual([]); // malformed entries are dropped, not surfaced
});

test("a failing localStorage never throws — append reports null, the send carries on", () => {
  vi.stubGlobal("localStorage", {
    getItem: () => {
      throw new DOMException("denied");
    },
    setItem: () => {
      throw new DOMException("QuotaExceededError");
    },
    removeItem: () => {
      throw new DOMException("denied");
    },
  });
  expect(() => readSent()).not.toThrow();
  expect(readSent()).toEqual([]);
  expect(appendSent({ text: "t", attachments: [], session: null })).toBeNull();
  expect(() => confirmSent("x")).not.toThrow();
  expect(() => clearSent()).not.toThrow();
});

test("an absent localStorage (sandboxed) degrades to a no-op safety net", () => {
  vi.stubGlobal("window", {
    get localStorage(): Storage {
      throw new DOMException("blocked");
    },
  });
  expect(readSent()).toEqual([]);
  expect(appendSent({ text: "t", attachments: [], session: null })).toBeNull();
});
