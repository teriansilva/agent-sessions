import { describe, expect, it } from "vitest";

import { ApiError } from "../../lib/api";
import type { McpOperation } from "../../types/mcp";
import { OPERATION_LOST, OperationEnded, POLL_MISSES, START_RETRIES, settleOperation, transient } from "./mcpOperation";

const op = (state: McpOperation["state"], extra: Partial<McpOperation<string>> = {}): McpOperation<string> => ({
  id: "rid",
  server: "s1",
  kind: "install",
  state,
  created_at: 0,
  updated_at: 0,
  result: null,
  error: null,
  ...extra,
});

/** An io whose start/poll answers come from queues (a function throws); counts every call. */
function io(starts: (McpOperation<string> | Error)[], polls: (McpOperation<string> | Error)[], alive = () => true) {
  const seen = { starts: [] as string[], polls: [] as string[], waits: 0 };
  const next = async (q: (McpOperation<string> | Error)[]) => {
    const a = q.shift();
    if (a === undefined) throw new Error("unexpected call");
    if (a instanceof Error) throw a;
    return a;
  };
  return {
    seen,
    io: {
      start: (rid: string) => (seen.starts.push(rid), next(starts)),
      poll: (rid: string) => (seen.polls.push(rid), next(polls)),
      wait: async () => (seen.waits++, alive()),
    },
  };
}

describe("settleOperation", () => {
  it("polls a running operation to done and resolves its result", async () => {
    const t = io([op("running")], [op("running"), op("done", { result: "summary" })]);
    await expect(settleOperation("rid", t.io)).resolves.toBe("summary");
    expect(t.seen.polls).toEqual(["rid", "rid"]);
  });

  it("a failed operation rejects with its words, as an ended operation", async () => {
    const t = io([op("running")], [op("failed", { error: "npm ci failed" })]);
    const e = await settleOperation("rid", t.io).catch((x: unknown) => x);
    expect(e).toBeInstanceOf(OperationEnded);
    expect((e as Error).message).toBe("npm ci failed");
    expect(transient(e)).toBe(false);
  });

  it("a 404 on a poll is the lost operation", async () => {
    const t = io([op("running")], [new ApiError(404, "no such operation")]);
    await expect(settleOperation("rid", t.io)).rejects.toThrow(OPERATION_LOST);
  });

  it("a 409 on start is the server's words and is not retried", async () => {
    const t = io([new ApiError(409, "this server is being reviewed or installed")], []);
    await expect(settleOperation("rid", t.io)).rejects.toThrow("this server is being reviewed or installed");
    expect(t.seen.starts).toHaveLength(1);
  });

  it("a lost start (no answer, or a gateway's) is sent again with the same id", async () => {
    const t = io([new TypeError("Failed to fetch"), new ApiError(504, "gateway"), op("done", { result: "r" })], []);
    await expect(settleOperation("rid", t.io)).resolves.toBe("r");
    expect(t.seen.starts).toEqual(["rid", "rid", "rid"]);
  });

  it("gives up on start after its retries, keeping the failure transient (the same id is reused)", async () => {
    const t = io(Array.from({ length: START_RETRIES + 1 }, () => new TypeError("Failed to fetch")), []);
    const e = await settleOperation("rid", t.io).catch((x: unknown) => x);
    expect(transient(e)).toBe(true);
    expect(t.seen.starts).toHaveLength(START_RETRIES + 1);
  });

  it("rides out polls with no answer, up to its limit", async () => {
    const blips = Array.from({ length: POLL_MISSES }, () => new TypeError("Failed to fetch"));
    const t = io([op("running")], [...blips, op("done", { result: "r" })]);
    await expect(settleOperation("rid", t.io)).resolves.toBe("r");
    const over = io([op("running")], Array.from({ length: POLL_MISSES + 1 }, () => new TypeError("Failed to fetch")));
    await expect(settleOperation("rid", over.io)).rejects.toThrow("Failed to fetch");
  });

  it("stops polling once the page is gone", async () => {
    const t = io([op("running")], [op("running")], () => false);
    await expect(settleOperation("rid", t.io)).resolves.toBeNull();
    expect(t.seen.polls).toEqual([]);
  });
});
