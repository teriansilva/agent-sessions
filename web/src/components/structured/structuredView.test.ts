import { describe, expect, test } from "vitest";
import type { StructuredRequest, StructuredSnapshot } from "../../types/api";
import {
  backgroundWorking,
  choiceLabel,
  cutBySendNow,
  interruptTarget,
  isActive,
  operationFor,
  operatorTurns,
  ordinal,
  ownTurnInterruptible,
  queuePositions,
  requestIds,
  requestRows,
  reviewOnly,
  runningAgentTurn,
  sendNowTarget,
} from "./structuredView";

function req(over: Partial<StructuredRequest>): StructuredRequest {
  return { request_id: "7", turn_id: "t", kind: "command", choices: ["approve", "reject", "cancel"], ...over };
}

test("every payload field becomes a row — unknown fields included, as JSON", () => {
  const rows = requestRows(
    req({
      payload: {
        command: ["npx", "playwright", "test"],
        cwd: "/w",
        reason: "verify",
        networkApprovalContext: { host: "registry.npmjs.org" },
        someFutureField: { nested: [1, 2] },
        threadId: "th1",
        itemId: "call_1",
      },
    }),
  );
  const keys = rows.map((r) => r.key);
  // Known fields first in a stable order, unknown ones after; correlation ids go to the footer.
  expect(keys).toEqual(["command", "cwd", "reason", "networkApprovalContext", "someFutureField"]);
  expect(rows[0]).toMatchObject({ label: "Command", kind: "code", text: "npx playwright test" });
  expect(rows.find((r) => r.key === "someFutureField")).toMatchObject({ kind: "json" });
  expect(rows.find((r) => r.key === "someFutureField")!.text).toContain('"nested"');
});

test("correlation ids are shown in the footer, never dropped", () => {
  const ids = requestIds(req({ payload: { threadId: "th1", itemId: "call_1", command: "ls" } }));
  expect(ids).toEqual(["thread Id th1", "item Id call_1"]);
});

test("a file change's patch renders as files; it is review-only when the server offers no approve", () => {
  const r = req({
    kind: "file_change",
    choices: ["reject", "cancel"],
    payload: { changes: [{ path: "a.ts", diff: "+x\n-y" }], reason: null },
  });
  const rows = requestRows(r);
  expect(rows[0]).toMatchObject({ key: "changes", kind: "patch", files: [{ path: "a.ts", diff: "+x\n-y" }] });
  expect(rows[1]).toMatchObject({ key: "reason", text: "none" });
  expect(reviewOnly(r)).toBe(true);
  expect(r.choices.map((c) => choiceLabel(c, r))).toEqual(["Decline", "Reject and stop the turn"]);
});

test("a Claude permission prompt shows its whole context, suggestions marked; standing grants come only from the server's bounded `always` list", () => {
  const r = req({
    kind: "Bash",
    payload: {
      tool_name: "Bash",
      input: { command: "git log" },
      blocked_path: null,
      decision_reason: "not covered by allow rules",
      title: "Claude wants to run a command",
      permission_suggestions: [{ type: "addRules" }],
    },
  });
  const keys = requestRows(r).map((x) => x.key);
  expect(keys).toEqual([
    "title",
    "tool_name",
    "input",
    "blocked_path",
    "decision_reason",
    "permission_suggestions",
  ]);
  expect(requestRows(r).find((x) => x.key === "permission_suggestions")!.kind).toBe("suggestions");
  const labels = r.choices.map((c) => choiceLabel(c, r));
  expect(labels).toEqual(["Allow once", "Deny", "Reject and stop the turn"]);
  expect(labels.join(" ")).not.toMatch(/always|session/i);
});

test("an over-long request the server could only present as text is still shown whole", () => {
  const rows = requestRows(req({ payload: "x".repeat(5000) }));
  expect(rows).toHaveLength(1);
  expect(rows[0].text).toHaveLength(5000);
});

test("a send reuses its operation id for the same text and mints a new one when it changes", () => {
  let n = 0;
  const mint = () => `id${++n}`;
  const first = operationFor(null, "hello", mint);
  expect(operationFor(first, "hello", mint)).toBe(first);
  expect(operationFor(first, "hello again", mint).id).toBe("id2");
});

test("the agent working with no operator turn running is shown, never doubled (#1430)", () => {
  const snap = (active_turn: string | null, background_active: boolean) =>
    ({ active_turn, turns: [], native: { native_id: "n", worker: "w", background_active } }) as unknown as StructuredSnapshot;
  expect(backgroundWorking(null)).toBe(false);
  expect(backgroundWorking(snap(null, true))).toBe(true);
  expect(backgroundWorking(snap(null, false))).toBe(false);
  expect(backgroundWorking(snap("op", true))).toBe(false); // the operator turn shows its own
  // A stale flag from a worker that has exited claims nothing.
  const exited = snap(null, true);
  exited.native!.worker = null;
  expect(backgroundWorking(exited)).toBe(false);
});

test("Send now targets the operator turn, else the agent's own turn its worker can interrupt (#1427)", () => {
  const snap = (over: Partial<StructuredSnapshot>, native: Partial<NonNullable<StructuredSnapshot["native"]>> = {}) =>
    ({ active_turn: null, turns: [], native: { native_id: "n", worker: "w", background_active: false, ...native }, ...over }) as unknown as StructuredSnapshot;
  expect(sendNowTarget(null)).toBeNull();
  // The operator turn wins, in the worker's own mode.
  expect(sendNowTarget(snap({ active_turn: "op" }, { send_now: "steer", background_turn: "bg", send_now_background: "interrupt" })))
    .toEqual({ turnId: "op", mode: "steer" });
  expect(sendNowTarget(snap({ active_turn: "op" }))).toBeNull(); // an old worker: no Send now at all
  // No operator turn: the agent's own turn, always interrupted.
  expect(sendNowTarget(snap({}, { send_now: "steer", background_turn: "bg", send_now_background: "interrupt" })))
    .toEqual({ turnId: "bg", mode: "interrupt" });
  // …only from a worker that advertises it, and only while that turn runs.
  expect(sendNowTarget(snap({}, { send_now: "interrupt", background_turn: "bg" }))).toBeNull();
  expect(sendNowTarget(snap({}, { send_now: "interrupt", background_turn: null, send_now_background: "interrupt" }))).toBeNull();
});

describe("a turn the agent runs for itself (#1438)", () => {
  const own = (state: string, turn_id = "own-1") =>
    ({ kind: "agent", turn_id, state, reply: "", reply_truncated: false, tools: [], tools_truncated: false }) as const;
  const op = (state: string, turn_id = "op-1") =>
    ({ kind: "operator", turn_id, operation_id: turn_id, state, text: "hi", reply: "", text_truncated: false,
      reply_truncated: false, reason: null, tools: [], tools_truncated: false }) as const;
  const snap = (turns: unknown[], native: Record<string, unknown> = {}, active_turn: string | null = null) =>
    ({ active_turn, state: "idle", turns, native: { native_id: "n", worker: "w", background_active: true,
      background_turn: "own-1", send_now_background: "interrupt", interrupt_own: true, ...native } }) as unknown as StructuredSnapshot;

  test("is shown as its turn, not as the background-task row, and reads live", () => {
    const running = snap([op("completed"), own("running")]);
    expect(runningAgentTurn(running)?.turn_id).toBe("own-1");
    expect(backgroundWorking(running)).toBe(false);
    expect(isActive(running)).toBe(true);
    const ended = snap([op("completed"), own("completed")]);
    expect(runningAgentTurn(ended)).toBeNull();
    expect(backgroundWorking(ended)).toBe(true); // a task still running: the generic row
    expect(isActive(ended)).toBe(false);
    // An exited worker runs nothing, whatever its last record said.
    expect(runningAgentTurn(snap([own("running")], { worker: null }))).toBeNull();
  });

  test("is interruptible only as the live worker's own target, from a worker that takes it", () => {
    const turn = own("running");
    expect(ownTurnInterruptible(snap([turn]), turn)).toBe(true);
    expect(ownTurnInterruptible(snap([turn], { interrupt_own: undefined }), turn)).toBe(false); // older worker
    expect(ownTurnInterruptible(snap([turn], { background_turn: "own-2" }), turn)).toBe(false); // a newer turn
    expect(ownTurnInterruptible(snap([turn], { send_now_background: undefined }), turn)).toBe(false);
    expect(ownTurnInterruptible(snap([own("completed")]), own("completed"))).toBe(false);
  });

  test("Esc stops the operator turn running now, else the agent's own, else nothing", () => {
    expect(interruptTarget(snap([op("running"), own("running")], {}, "op-1"))).toBe("op-1");
    expect(interruptTarget(snap([op("awaiting_approval")], {}, "op-1"))).toBe("op-1");
    expect(interruptTarget(snap([op("completed"), own("running")]))).toBe("own-1");
    expect(interruptTarget(snap([op("completed"), own("running")], { interrupt_own: false }))).toBeNull();
    expect(interruptTarget(snap([op("completed"), own("completed")]))).toBeNull();
    expect(interruptTarget(null)).toBeNull();
  });

  test("is never mistaken for a message someone sent", () => {
    expect(operatorTurns(snap([op("completed"), own("running"), op("queued", "op-2")])).map((t) => t.turn_id))
      .toEqual(["op-1", "op-2"]);
  });
});

describe("Send now keeps the queue's order (#1442)", () => {
  const op = (turn_id: string, state: string, extra: Record<string, unknown> = {}) =>
    ({ kind: "operator", turn_id, operation_id: turn_id, state, text: turn_id, reply: "", text_truncated: false,
      reply_truncated: false, reason: null, tools: [], tools_truncated: false, ...extra }) as const;
  const snap = (turns: unknown[]) => ({ active_turn: null, turns }) as unknown as StructuredSnapshot;

  test("positions count queued operator messages in order; agent turns and settled ones don't count", () => {
    const s = snap([op("a", "completed"), op("b", "running"), op("c", "queued"),
      { kind: "agent", turn_id: "own", state: "running", tools: [] }, op("d", "queued"), op("e", "queued")]);
    expect([...queuePositions(s)]).toEqual([["c", 1], ["d", 2], ["e", 3]]);
    expect(queuePositions(null).size).toBe(0);
  });

  test("ordinals read as English", () => {
    expect([1, 2, 3, 4, 11, 12, 13, 21, 22, 101, 111].map(ordinal))
      .toEqual(["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st", "111th"]);
  });

  test("a turn is cut by Send now only when it settled interrupted and a sent interrupt named it", () => {
    const cut = op("t", "interrupted");
    expect(cutBySendNow(snap([cut, op("q", "running", { delivery_turn_id: "t" })]), cut)).toBe(true);
    // Still waiting to go (Hermes on #1451): nothing is "answered below" yet.
    expect(cutBySendNow(snap([cut, op("q", "queued", { delivery: "interrupting", delivery_turn_id: "t" })]), cut)).toBe(false);
    // A steer cuts nothing; a refusal still waiting says so; nothing named it; or it wasn't interrupted.
    expect(cutBySendNow(snap([cut, op("q", "delivered", { delivery: "steered", delivery_turn_id: "t" })]), cut)).toBe(false);
    expect(cutBySendNow(snap([cut, op("q", "queued", { delivery: "interrupt_failed", delivery_turn_id: "t" })]), cut)).toBe(false);
    expect(cutBySendNow(snap([cut, op("q", "running")]), cut)).toBe(false);
    // The message itself never went (Hermes on #1451): the turn is plainly interrupted.
    expect(cutBySendNow(snap([cut, op("q", "failed", { delivery_turn_id: "t" })]), cut)).toBe(false);
    const done = op("t", "completed");
    expect(cutBySendNow(snap([done, op("q", "running", { delivery_turn_id: "t" })]), done)).toBe(false);
  });
});
