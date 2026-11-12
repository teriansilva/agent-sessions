import { describe, expect, test } from "vitest";

import fixture from "./fixtures/envelopes.json";
import { openEnvelope, parseLink, WrongKeyError, type ReplyEnvelope } from "./seal";

// Sealed by `src/agent_sessions/reply_share.py`, opened here: the two implementations of the
// format can't drift (#1956). `tests/test_reply_share.py` opens the same file in Python.
describe("reply envelopes", () => {
  for (const c of fixture.cases) {
    test(`opens: ${c.name}`, async () => {
      const env = c.envelope as ReplyEnvelope;
      const got = await openEnvelope(env, c.key, "password" in c ? (c.password as string) : null);
      expect(got).toEqual(c.content);
    }, 20_000);
  }

  test("a wrong password or key is a WrongKeyError, never garbage", async () => {
    const withPw = fixture.cases.find((c) => "password" in c)!;
    await expect(openEnvelope(withPw.envelope as ReplyEnvelope, withPw.key, "nope")).rejects.toBeInstanceOf(
      WrongKeyError,
    );
    const plain = fixture.cases.find((c) => !("password" in c))!;
    const other = "A".repeat(43);
    await expect(openEnvelope(plain.envelope as ReplyEnvelope, other)).rejects.toBeInstanceOf(WrongKeyError);
    await expect(openEnvelope(plain.envelope as ReplyEnvelope, "short")).rejects.toBeInstanceOf(WrongKeyError);
  }, 20_000);

  test("a moved expiry does not open", async () => {
    const plain = fixture.cases.find((c) => !("password" in c))!;
    const moved = { ...(plain.envelope as ReplyEnvelope), expires_at: 4_000_000_000 };
    await expect(openEnvelope(moved, plain.key)).rejects.toBeInstanceOf(WrongKeyError);
  });

  test("the link's id is in the path and its key in the fragment", () => {
    const id = "Q3J9AAAAAAAAAAAAAAAAAA";
    expect(parseLink({ pathname: `/r/${id}`, hash: "#k=abc" })).toEqual({ id, key: "abc" });
    expect(parseLink({ pathname: `/r/${id}/`, hash: "" })).toEqual({ id, key: null });
    expect(parseLink({ pathname: "/r/short", hash: "#k=abc" })).toEqual({ id: null, key: "abc" });
  });
});
