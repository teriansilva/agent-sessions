import { describe, expect, it } from "vitest";

import { failedNow, readsLabel, statusLine, testFailure, type ConnectorInstance } from "./connectors";

function inst(over: Partial<ConnectorInstance> = {}): ConnectorInstance {
  return {
    id: "c_1",
    manifest_id: "forgejo",
    origin: "https://git.example.com",
    status: "ready",
    token_set: true,
    generation: 3,
    tested_generation: 3,
    tested: { login: "commander", repos: 4, at: 1 },
    last_test: { generation: 3, ok: true, code: null, at: 1 },
    enabled: { read: false },
    created_at: 1,
    ...over,
  };
}

describe("statusLine", () => {
  it("is up only while HQ reads through the connector", () => {
    expect(statusLine(inst({ enabled: { read: true } }))).toEqual({ tone: "up", text: "HQ reads through it" });
    expect(statusLine(inst())).toEqual({ tone: "idle", text: "Tested · reading is off" });
  });

  it("reads setup steps as draft, never as a status that needs you", () => {
    expect(statusLine(inst({ status: "needs_origin", origin: "" })).tone).toBe("draft");
    expect(statusLine(inst({ status: "needs_token" })).text).toBe("Needs a token");
    expect(statusLine(inst({ status: "needs_test", last_test: null })).text).toBe("Needs a test");
  });

  it("is down for a failed test of what is stored now, or a manifest that is gone", () => {
    const failed = { generation: 3, ok: false, code: "rejected", at: 2 };
    expect(statusLine(inst({ status: "needs_test", last_test: failed }))).toEqual({
      tone: "down",
      text: "Test failed",
    });
    expect(statusLine(inst({ status: "unavailable" })).tone).toBe("down");
  });
});

describe("failedNow", () => {
  it("ignores a failure from before the last edit", () => {
    const old = { generation: 2, ok: false, code: "rejected", at: 1 };
    expect(failedNow(inst({ status: "needs_test", last_test: old }))).toBe(false);
    expect(failedNow(inst({ last_test: { ...old, generation: 3 } }))).toBe(true);
  });
});

describe("testFailure", () => {
  it("prefers the server's own words, then the code's, then a fallback", () => {
    expect(testFailure("rejected", "git.example.com rejected the token (401)")).toBe(
      "git.example.com rejected the token (401)",
    );
    expect(testFailure("timeout")).toBe("The forge did not answer in time.");
    expect(testFailure("something_new")).toBe("The test did not pass.");
    expect(testFailure(null)).toBe("The test did not pass.");
  });
});

describe("readsLabel", () => {
  it("names each capability and passes an unknown one through", () => {
    expect(readsLabel(["repos", "pulls", "ci", "wiki"])).toBe("repositories · pull requests · CI status · wiki");
  });
});
