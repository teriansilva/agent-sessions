import { expect, test } from "vitest";

import { confirmPhrase, settledWord } from "./cardVoice";

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
