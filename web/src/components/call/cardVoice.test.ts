import { expect, test } from "vitest";

import { confirmPhrase, listeningFor, settledWord } from "./cardVoice";

test("one card: say confirm; several: say which (#1397 P2)", () => {
  expect(confirmPhrase(0, 1)).toBe("confirm");
  expect(confirmPhrase(1, 3)).toBe("confirm the second");
});

test("the stamp comes from the route's answer, never a guess", () => {
  expect(settledWord({ ok: true, status: 200 }, "nudge")).toEqual({ text: "✓ Sent by voice", tone: "ok" });
  expect(settledWord({ ok: true, status: 200 }, "operation").text).toBe("✓ Done by voice");
  expect(settledWord({ ok: true, status: 202 }, "session")).toEqual({ text: "Accepted · starting", tone: "warn" });
  expect(settledWord({ ok: true, status: 202, final: "started" }, "session").text).toBe("✓ Started");
  expect(settledWord({ ok: true, status: 202, final: null }, "session").text).toBe("Still running");
  expect(settledWord({ ok: false, status: 409 }, "nudge")).toEqual({ text: "Changed — not sent", tone: "alert" });
  expect(settledWord({ ok: false, status: 0 }, "nudge").text).toBe("Didn't go through");
});

test("a spoken dismiss reads as such, in no success colour (#2120)", () => {
  expect(settledWord({ ok: true, status: 200, dismissed: true }, "session")).toEqual({
    text: "Dismissed by voice",
    tone: "neutral",
  });
  // A dismiss the route did not carry out is a failure like any other.
  expect(settledWord({ ok: false, status: 409, dismissed: true }, "session").tone).toBe("alert");
});

test("only HQ's own cards are listened to for a dismiss (#2120)", () => {
  expect(listeningFor("session")).toBe("“confirm”, “dismiss” or “cancel”");
  expect(listeningFor("operation")).toBe("“confirm”, “dismiss” or “cancel”");
  expect(listeningFor("nudge")).toBe("“confirm” or “cancel”");
  expect(listeningFor("needs_you")).toBe("“confirm” or “cancel”");
});

test("a batch card whose route gave no outcome in time is unknown, never failed (#2389)", () => {
  expect(settledWord({ ok: false, status: 0, unknown: true }, "operation")).toEqual({
    text: "Can't tell — not retried",
    tone: "warn",
  });
});
