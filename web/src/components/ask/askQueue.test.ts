import { describe, expect, it } from "vitest";

import {
  ASK_QUEUE_MAX,
  queueLabel,
  queueLine,
  queueOffer,
  queueStateOf,
  queuedCount,
  seenIsOwed,
  shownThrough,
} from "./askQueue";

const running = { pending: true, error: null, queue: null } as const;
const queued = { pending: false, error: null, queue: "queued" } as const;
const done = { pending: false, error: null, queue: null } as const;

describe("the queue behind a running HQ turn (#2103)", () => {
  it("reads a turn's queue state from its server status", () => {
    expect(queueStateOf("queued")).toBe("queued");
    expect(queueStateOf("withdrawn")).toBe("withdrawn");
    expect(queueStateOf("not_started")).toBe("not_started");
    for (const s of ["pending", "done", "failed", "stopped", "anything"])
      expect(queueStateOf(s)).toBeNull();
  });

  it("offers nothing special while no turn runs", () => {
    expect(queueOffer([])).toBe("idle");
    expect(queueOffer([done])).toBe("idle");
    // A turn that failed locally is not running: nothing can queue behind it.
    expect(queueOffer([{ pending: true, error: "That didn't work.", queue: null }])).toBe("idle");
    expect(queueLine([done])).toBeNull();
  });

  it("keeps Send open behind a running turn and says so once something is queued", () => {
    expect(queueOffer([running])).toBe("open");
    expect(queueLine([running])).toBeNull();
    expect(queueLine([running, queued])).toEqual({
      text: "Queued behind the running question",
      warn: false,
    });
  });

  it("closes Send when the queue is full", () => {
    const full = [running, ...Array.from({ length: ASK_QUEUE_MAX }, () => queued)];
    expect(queuedCount(full)).toBe(3);
    expect(queueOffer(full)).toBe("full");
    expect(queueLine(full)).toEqual({ text: "Queue full — withdraw one or wait", warn: true });
    // A withdrawn or unstarted one holds no slot.
    const freed = [running, queued, queued, { ...done, queue: "withdrawn" as const }];
    expect(queueOffer(freed)).toBe("open");
  });

  it("words each state", () => {
    expect(queueLabel("queued")).toBe("Queued");
    expect(queueLabel("withdrawn")).toBe("Withdrawn");
    expect(queueLabel("not_started")).toBe("Not started — the server restarted");
  });
});

describe("seen means the settlement was displayed (#2103)", () => {
  it("marks through the newest update and the newest SETTLEMENT on screen", () => {
    expect(shownThrough([], [])).toBe(0);
    expect(shownThrough([{ ts: 5 }, { ts: 3 }], [])).toBe(5);
    expect(shownThrough([{ ts: 5 }], [{ settledAt: 9 }, { settledAt: 7 }])).toBe(9);
  });

  it("never counts a turn that is still queued or running", () => {
    // Asked at 20, queued; nothing has settled: nothing newer than the update is seen.
    expect(shownThrough([{ ts: 5 }], [{ settledAt: null }, {}])).toBe(5);
    expect(shownThrough([], [{ settledAt: null }])).toBe(0);
  });

  it("owes a mark only for something newer than the cursor, or an unread update", () => {
    expect(seenIsOwed(0, 0, false)).toBe(false);
    expect(seenIsOwed(9, 9, false)).toBe(false);
    expect(seenIsOwed(9, 12, false)).toBe(false);
    expect(seenIsOwed(9, 7, false)).toBe(true);
    expect(seenIsOwed(9, 9, true)).toBe(true);
    expect(seenIsOwed(0, 0, true)).toBe(false);
  });
});
