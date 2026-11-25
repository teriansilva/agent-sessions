import { expect, test } from "vitest";
import { draftSessionKey } from "./draftSessionKey";

test("a draft lives under the DURABLE key: the converged rowKey, never the frozen placeholder (#908 round 5)", () => {
  // A placeholder before the converge: nowhere to keep a draft yet.
  expect(draftSessionKey("opencode", "new-abc", "opencode:new-abc")).toBeNull();
  expect(draftSessionKey("opencode", "new-abc")).toBeNull();
  // After the converge the terminal identity is still the placeholder; the rowKey is real.
  expect(draftSessionKey("opencode", "new-abc", "opencode:ses_real0000")).toBe("opencode:ses_real0000");
  // No placeholder in the first place.
  expect(draftSessionKey("claude", "abc123", "claude:abc123")).toBe("claude:abc123");
  expect(draftSessionKey("claude", "abc123")).toBe("claude:abc123");
  // Malformed rowKeys fall back to the frozen identity rather than minting a bogus key.
  expect(draftSessionKey("claude", "abc123", ":")).toBe("claude:abc123");
  expect(draftSessionKey("claude", "abc123", "")).toBe("claude:abc123");
  expect(draftSessionKey("", "", ":")).toBeNull();
});
