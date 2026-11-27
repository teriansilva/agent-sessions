/** The automation queue's words (#2422): the same sentences on every surface. */
import { describe, expect, test } from "vitest";

import {
  effectiveReserve,
  hhmm,
  ordinal,
  queueHeader,
  queueLine,
  queueNoticeBody,
  queueOutcome,
  reserveHelp,
  reserveProblem,
} from "./automationQueue";
import { outcomeWord, stepWords } from "./automations";

const Q = {
  position: 2,
  since: 1_795_000_000,
  capacity: "session_limit",
  detail: "the session limit is full (6 of 6 running)",
};

describe("ordinal", () => {
  test.each([
    [1, "1st"],
    [2, "2nd"],
    [3, "3rd"],
    [4, "4th"],
    [10, "10th"],
    [11, "11th"],
    [12, "12th"],
    [13, "13th"],
    [21, "21st"],
    [22, "22nd"],
    [23, "23rd"],
    [101, "101st"],
    [111, "111th"],
    [112, "112th"],
  ])("%i → %s", (n, word) => expect(ordinal(n)).toBe(word));
});

describe("a queued run", () => {
  test("the row says what it waits for, since when and where it stands", () => {
    expect(queueLine(Q, () => "21:14")).toBe("Queued for a free slot · since 21:14 · 2nd in line");
    expect(queueLine(Q)).toBe(`Queued for a free slot · since ${hhmm(Q.since)} · 2nd in line`);
  });

  test("the detail's outcome", () => {
    expect(queueOutcome({ ...Q, position: 1 })).toBe("Queued for a free slot · 1st in line");
  });

  test("the notice repeats the server's reason and says nothing has failed", () => {
    expect(queueNoticeBody(Q)).toBe(
      "The session limit is full (6 of 6 running). This step starts on its own as soon as a slot frees; nothing has failed.",
    );
    // A trailing full stop from the server is not doubled.
    expect(queueNoticeBody({ ...Q, detail: "the pool is full." })).toMatch(/^The pool is full\. This/);
    expect(queueNoticeBody({ ...Q, capacity: "concurrency", detail: "" })).toMatch(
      /^The previous run of this automation is still working\. /,
    );
  });

  test("reads Queued, grey, and its step is labelled", () => {
    expect(outcomeWord("", "queued")).toEqual({ word: "Queued", tone: "idle" });
    expect(stepWords("queued")).toBe("Queued for a free slot");
  });
});

describe("the list header", () => {
  test("shows the length, and nothing for an empty or unknown queue", () => {
    expect(queueHeader(3)).toBe("3 queued · waiting for a free slot");
    expect(queueHeader(0)).toBe("");
    expect(queueHeader(undefined)).toBe("");
  });
});

describe("the reservation's help line", () => {
  test("says what is left for every other start", () => {
    expect(reserveHelp(6, 1, "pool")).toBe(
      "Other starts can use 5 of 6; automations can use any free slot.",
    );
    expect(reserveHelp(6, 1, "sessions")).toBe(
      "Other starts can use 5 of 6; automations can use any free slot.",
    );
  });

  test("says plainly when only automations can start", () => {
    expect(reserveHelp(1, 1, "pool")).toBe(
      "Only automations can start here: the whole pool of 1 is reserved for them.",
    );
    expect(reserveHelp(4, 4, "sessions")).toBe(
      "Only automations can start sessions: all 4 are reserved for them.",
    );
    // A stored reserve above the limit is only in force up to it.
    expect(effectiveReserve(5, 3)).toBe(3);
    expect(reserveHelp(3, 5, "pool")).toMatch(/^Only automations/);
  });

  test("refuses a reserve above the limit, with the reason", () => {
    expect(reserveProblem(3, 4, "pool limit")).toBe("The reserve can't be more than the pool limit (3).");
    expect(reserveProblem(3, 3, "pool limit")).toBe("");
  });
});
