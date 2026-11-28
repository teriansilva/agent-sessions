/** A task's subagents (#2398): the group's states, the Commander's own controls and the driver
 *  badge's words. Layout, 44px targets and focus are the real-browser spec's
 *  (`e2e/hq-subagents.spec.ts`). */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { act } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { AskSubagent } from "../../types/api";
import { driverLabel } from "../sessions/driverLabel";
import { SubagentGroup } from "./SubagentGroup";
import { IDLE_POLL_MS, useLiveSubagents } from "./useLiveSubagents";

vi.mock("../../lib/api", () => ({
  ApiError: class ApiError extends Error {
    status = 0;
  },
  api: {
    askSubagents: vi.fn(),
    askCancelSubagent: vi.fn(),
    handBackDriver: vi.fn(),
    structuredInterrupt: vi.fn(),
  },
}));

const CID = "c".repeat(32);
const TURN = "11111111-1111-4111-8111-111111111111";

const ROWS: AskSubagent[] = [
  {
    turn_id: TURN, handle: "sp-1", outcome: "started", title: "web-shop",
    agent: "Claude Code · Researcher v3", key: "claude:aaaa", task_n: 2, state: "working",
    active_turn: "t-1", driver: { who: "hq", generation: 1, here: true }, steered: 2,
  },
  {
    turn_id: TURN, handle: "sp-2", outcome: "started", title: "web-shop",
    agent: "Codex · Tester v2", key: "codex:bbbb", task_n: 2, state: "taken_over",
    driver: { who: "commander", generation: 3, here: true }, steered: 0,
  },
  {
    turn_id: TURN, handle: "sp-3", outcome: "queued", title: "web-shop",
    agent: "Claude Code · Reviewer v1", task_n: 2, reason: "queued: session limit 6 / 6",
  },
];

beforeEach(() => {
  vi.mocked(api.askSubagents).mockResolvedValue({ subagents: ROWS });
  vi.mocked(api.askCancelSubagent).mockResolvedValue({ handle: "sp-3", outcome: "cancelled" });
  vi.mocked(api.handBackDriver).mockResolvedValue({} as never);
  vi.mocked(api.structuredInterrupt).mockResolvedValue({});
});

/** The conversation as AskConsole wires it: ONE live poller, every group fed from it. */
function Harness({ stored }: { stored: AskSubagent[] }) {
  const live = useLiveSubagents(CID, stored);
  return (
    <SubagentGroup cid={CID} turnId={TURN} rows={live.rows} taskN={2} onChanged={live.refresh} />
  );
}

function renderGroup(rows = ROWS) {
  return render(
    <MemoryRouter>
      <Harness stored={rows} />
    </MemoryRouter>,
  );
}

test("each subagent shows its state and only the controls that apply", async () => {
  renderGroup();
  const rows = await screen.findAllByTestId("ask-subagent");
  expect(rows.map((r) => r.dataset.state)).toEqual(["working", "taken_over", "queued"]);
  expect(rows[0]).toHaveTextContent("steered 2×");
  expect(rows[2]).toHaveTextContent("session limit 6 / 6");
  // Working: Take over + Stop. Taken over: Open + Hand back. Queued: Cancel only.
  expect(screen.getAllByTestId("ask-subagent-open").map((b) => b.textContent)).toEqual([
    "Take over",
    "Open",
  ]);
  expect(screen.getAllByTestId("ask-subagent-stop")).toHaveLength(1);
  expect(screen.getAllByTestId("ask-subagent-hand-back")).toHaveLength(1);
  expect(screen.getAllByTestId("ask-subagent-cancel")).toHaveLength(1);
});

test("Stop, Hand back and Cancel are the Commander's own routes", async () => {
  renderGroup();
  await screen.findAllByTestId("ask-subagent");
  await userEvent.click(screen.getByTestId("ask-subagent-stop"));
  await waitFor(() => expect(api.structuredInterrupt).toHaveBeenCalled());
  expect(vi.mocked(api.structuredInterrupt).mock.calls[0][0]).toBe("claude:aaaa");
  expect(vi.mocked(api.structuredInterrupt).mock.calls[0][2]).toBe("t-1");
  await userEvent.click(screen.getByTestId("ask-subagent-hand-back"));
  await waitFor(() => expect(api.handBackDriver).toHaveBeenCalledWith("codex:bbbb", 3));
  await userEvent.click(screen.getByTestId("ask-subagent-cancel"));
  await waitFor(() => expect(api.askCancelSubagent).toHaveBeenCalledWith(CID, TURN, "sp-3"));
});

test("a refused tap says so and keeps the rows", async () => {
  vi.mocked(api.askCancelSubagent).mockRejectedValue(new Error("it started meanwhile"));
  renderGroup();
  await screen.findAllByTestId("ask-subagent");
  await userEvent.click(screen.getByTestId("ask-subagent-cancel"));
  expect(await screen.findByTestId("ask-subagents-error")).toHaveTextContent(
    "Not cancelled: it started meanwhile",
  );
});

test("the driver badge says HQ and its task, You, or Mission", () => {
  expect(driverLabel({ who: "hq", task_label: "task 2" })).toBe("HQ · task 2");
  expect(driverLabel({ who: "hq" })).toBe("HQ");
  expect(driverLabel({ who: "commander" })).toBe("You");
  expect(driverLabel({ who: "mission" })).toBe("Mission");
});

// ---- extant sessions stay observed (Hermes 9131) --------------------------------------------------

afterEach(() => {
  vi.useRealTimers();
});

const ONE = (over: Partial<AskSubagent>): AskSubagent[] => [
  {
    turn_id: TURN, handle: "sp-1", outcome: "started", title: "web-shop",
    agent: "Claude Code · Researcher v3", key: "claude:aaaa", task_n: 2,
    driver: { who: "hq", generation: 1, here: true }, steered: 0, active_turn: null, ...over,
  },
];

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

test.each([
  ["waiting", { state: "waiting" }],
  ["done", { state: "done" }],
])("a %s subagent is still observed: it working again brings Stop back", async (_name, from) => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.askSubagents).mockResolvedValue({ subagents: ONE(from) });
  renderGroup(ONE(from));
  await advance(0);
  expect(screen.getByTestId("ask-subagent").dataset.state).toBe(from.state);
  expect(screen.queryByTestId("ask-subagent-stop")).toBeNull();
  vi.mocked(api.askSubagents).mockResolvedValue({
    subagents: ONE({ state: "working", active_turn: "t-9" }),
  });
  await advance(IDLE_POLL_MS + 100);
  expect(screen.getByTestId("ask-subagent").dataset.state).toBe("working");
  expect(screen.getByTestId("ask-subagent-stop")).toBeInTheDocument();
});

test("a driver change is seen: the Commander's take-over offers Hand back", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const done = ONE({ state: "done" });
  vi.mocked(api.askSubagents).mockResolvedValue({ subagents: done });
  renderGroup(done);
  await advance(0);
  expect(screen.queryByTestId("ask-subagent-hand-back")).toBeNull();
  vi.mocked(api.askSubagents).mockResolvedValue({
    subagents: ONE({ state: "taken_over", driver: { who: "commander", generation: 2, here: true } }),
  });
  await advance(IDLE_POLL_MS + 100);
  expect(screen.getByTestId("ask-subagent-hand-back")).toBeInTheDocument();
});

test.each([
  ["starting", { state: "starting" }],
  ["unreadable", { state: "unreadable" }],
])("a %s subagent keeps being observed until it reads working", async (_name, from) => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.askSubagents).mockResolvedValue({ subagents: ONE(from) });
  renderGroup(ONE({ state: undefined }));
  await advance(0);
  expect(screen.getByTestId("ask-subagent").dataset.state).toBe(from.state);
  vi.mocked(api.askSubagents).mockResolvedValue({
    subagents: ONE({ state: "working", active_turn: "t-9" }),
  });
  await advance(IDLE_POLL_MS + 100);
  expect(screen.getByTestId("ask-subagent").dataset.state).toBe("working");
  expect(screen.getByTestId("ask-subagent-stop")).toBeInTheDocument();
});

test("a session the runtime says is gone is no longer polled", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.askSubagents).mockResolvedValue({ subagents: ONE({ state: "gone" }) });
  renderGroup(ONE({ state: undefined }));
  await advance(0);
  const calls = vi.mocked(api.askSubagents).mock.calls.length;
  await advance(IDLE_POLL_MS * 3);
  expect(vi.mocked(api.askSubagents).mock.calls.length).toBe(calls);
});

test("handles that never had a session are not polled", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const ended = [{ ...ROWS[2], outcome: "not_started" as const, state: "not_started" }];
  vi.mocked(api.askSubagents).mockResolvedValue({ subagents: ended });
  renderGroup(ended);
  await advance(0);
  const calls = vi.mocked(api.askSubagents).mock.calls.length;
  await advance(IDLE_POLL_MS * 3);
  expect(vi.mocked(api.askSubagents).mock.calls.length).toBe(calls);
});
