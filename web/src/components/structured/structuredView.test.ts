import { expect, test } from "vitest";
import type { StructuredRequest, StructuredSnapshot } from "../../types/api";
import { choiceLabel, operationFor, requestIds, requestRows, reviewOnly, sendNowTarget } from "./structuredView";

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
