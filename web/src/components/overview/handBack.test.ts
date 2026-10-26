/** #1648: a refused request is handed back to its full-screen route; only a CAP refusal says the
 *  map was full. */
import { expect, test } from "vitest";
import { handBack } from "./handBack";

const seed = { key: "claude:abc", engine: "claude", id: "abc", title: "New session" };
const fresh = { cwd: "/w", bypass: true };

test("a cap refusal carries its launch params and says the map was full", () => {
  expect(handBack({ seed, fresh, reason: "cap" })).toEqual({
    to: "/s/claude/abc",
    state: { fresh, mapFull: true },
  });
});

test("a map that could not host is not 'full': no notice, launch params intact", () => {
  expect(handBack({ seed, fresh, reason: "host" })).toEqual({ to: "/s/claude/abc", state: { fresh } });
});

test("a refusal without launch params carries no state at all", () => {
  expect(handBack({ seed, reason: "host" })).toEqual({ to: "/s/claude/abc", state: {} });
});

test("the route segments are encoded", () => {
  expect(handBack({ seed: { ...seed, id: "ses_a/b" }, reason: "cap" }).to).toBe("/s/claude/ses_a%2Fb");
});
