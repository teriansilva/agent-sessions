/** The failure block and the header's Start again, mounted with the real hook (#966 P2, #967 P4).
 *
 *  The browser spec (`e2e/mission-thread-events.spec.ts`) owns geometry and the full flow; these pin the
 *  rules a unit can: the detail decides, only the newest failure acts, both entry points share one
 *  request, and a refusal is shown in the server's words. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { ApiError, api } from "../../lib/api";
import type { Mission, MissionEvent } from "../../types/api";

import { MissionHeaderActions } from "./MissionHeaderActions";
import { MissionThreadEvents } from "./MissionThreadEvent";
import { useStartAgain } from "./useStartAgain";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      setMissionState: vi.fn(),
      archiveMission: vi.fn(),
      unarchiveMission: vi.fn(),
    },
  };
});

const setState = vi.mocked(api.setMissionState);

function failure(seq: number, retryEligibleSnapshot = true): MissionEvent {
  return {
    seq,
    mission_id: "msn_1",
    at: 1_700_000_000 + seq,
    kind: "state",
    session_key: null,
    action_id: null,
    text: "dispatching -> failed: never ready",
    meta: {
      from: "dispatching",
      to: "failed",
      detail: "never ready",
      session_key: "claude:abc",
      seed_outcome: "not_attempted",
      teardown_confirmed: true,
      retry_eligible: retryEligibleSnapshot,
      message: `failure ${seq}`,
    },
    settlement: null,
  };
}

function mission(over: Partial<Mission> = {}): Mission {
  return {
    id: "msn_1",
    title: "t",
    instruction: null,
    brief: null,
    project_id: "p1",
    cwd: "/repo",
    engine: null,
    engine_source: null,
    state: "failed",
    playbook_id: null,
    created_at: 1,
    updated_at: 1,
    closed_at: 1,
    archived_at: null,
    archiving_at: null,
    unarchiving_at: null,
    outcome: "failed",
    sessions: [],
    ...over,
  };
}

function Harness({
  m,
  events,
  onChanged = () => {},
}: {
  m: Mission;
  events: MissionEvent[];
  onChanged?: () => void;
}) {
  const startAgain = useStartAgain(m, { onChanged });
  return (
    <MemoryRouter>
      <MissionHeaderActions
        mission={m}
        startAgain={startAgain}
        onChanged={() => {}}
        onNote={() => {}}
      />
      <MissionThreadEvents
        events={events}
        mission={m}
        startAgain={startAgain}
        objectives={[]}
        projectNames={{}}
      />
    </MemoryRouter>
  );
}

beforeEach(() => {
  setState.mockReset();
});

test("an eligible detail offers Start again in the header AND on the newest failure only", async () => {
  render(<Harness m={mission({ retry_eligible: true })} events={[failure(9), failure(3)]} />);
  const blocks = screen.getAllByTestId("thread-failure");
  expect(blocks).toHaveLength(2);
  expect(within(blocks[0]).getByTestId("thread-start-again")).toBeTruthy();
  expect(within(blocks[0]).getByTestId("thread-open-session").textContent).toBe("Open session log");
  expect(within(blocks[1]).queryAllByRole("button")).toHaveLength(0);
  expect(within(blocks[1]).queryAllByRole("link")).toHaveLength(0);
  expect(screen.getByTestId("mission-start-again")).toBeTruthy();
  expect(screen.queryByTestId("mission-reopen")).toBeNull();
});

test("the detail decides: a snapshot saying eligible does not offer Start again", async () => {
  render(
    <Harness
      m={mission({ retry_eligible: false, retry_reason: "the brief was typed" })}
      events={[failure(9, true)]}
    />,
  );
  expect(screen.queryByTestId("thread-start-again")).toBeNull();
  expect(screen.queryByTestId("mission-start-again")).toBeNull();
  // The lifecycle's own primary is untouched.
  expect(screen.getByTestId("mission-reopen")).toBeTruthy();
  expect(screen.getByTestId("thread-open-session").textContent).toBe("Open session");
  const why = screen.getByRole("button", { name: "Why no retry?" });
  expect(why.getAttribute("aria-expanded")).toBe("false");
  await userEvent.click(why);
  expect(why.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByTestId("thread-retry-reason").textContent).toBe("the brief was typed");
});

test("a mission that is no longer failed shows its failures as a record, with no actions", () => {
  render(<Harness m={mission({ state: "planned", retry_eligible: false })} events={[failure(9)]} />);
  const block = screen.getByTestId("thread-failure");
  expect(within(block).queryAllByRole("button")).toHaveLength(0);
  expect(within(block).queryAllByRole("link")).toHaveLength(0);
});

test("both entry points send ONE `failed -> planned`, disabled while it is in flight", async () => {
  let release!: () => void;
  setState.mockImplementation(
    () => new Promise((resolve) => (release = () => resolve(mission({ state: "planned" })))),
  );
  const onChanged = vi.fn();
  render(
    <Harness m={mission({ retry_eligible: true })} events={[failure(9)]} onChanged={onChanged} />,
  );
  await userEvent.click(screen.getByTestId("thread-start-again"));
  await waitFor(() =>
    expect((screen.getByTestId("mission-start-again") as HTMLButtonElement).disabled).toBe(true),
  );
  expect((screen.getByTestId("thread-start-again") as HTMLButtonElement).disabled).toBe(true);
  await userEvent.click(screen.getByTestId("mission-start-again"));
  expect(setState).toHaveBeenCalledTimes(1);
  expect(setState).toHaveBeenCalledWith("msn_1", { from: "failed", to: "planned" });
  release();
  await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  // Still disabled: the detail read that follows has not landed, and a second press could only 409.
  expect((screen.getByTestId("thread-start-again") as HTMLButtonElement).disabled).toBe(true);
});

test("a 409 is shown in the server's words, in the block, and the detail is re-read", async () => {
  setState.mockRejectedValue(
    new ApiError(409, "mission msn_1 cannot be started again: the failed session could not be confirmed stopped"),
  );
  const onChanged = vi.fn();
  const m = mission({ retry_eligible: true });
  const { rerender } = render(<Harness m={m} events={[failure(9)]} onChanged={onChanged} />);
  await userEvent.click(screen.getByTestId("mission-start-again"));
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toBe(
    "mission msn_1 cannot be started again: the failed session could not be confirmed stopped",
  );
  expect(onChanged).toHaveBeenCalledTimes(1);
  // A NEW detail object releases the controls; the words stay until the next attempt.
  rerender(<Harness m={{ ...m }} events={[failure(9)]} onChanged={onChanged} />);
  await waitFor(() =>
    expect((screen.getByTestId("thread-start-again") as HTMLButtonElement).disabled).toBe(false),
  );
  expect(screen.getByRole("alert").textContent).toContain("could not be confirmed stopped");
});

test("a question notice (`error`) is a compact error row: no chips, no actions, no live alert, even on an eligible failed mission", () => {
  const notice: MissionEvent = {
    ...failure(12),
    kind: "error",
    text: "No question could be asked: the reply offered no usable options.",
    meta: { objective: "pr" },
  };
  render(<Harness m={mission({ retry_eligible: true })} events={[notice, failure(9)]} />);
  const row = screen.getByTestId("thread-error");
  expect(row.getAttribute("aria-label")).toBe("Error");
  expect(within(row).getByTestId("thread-error-text").textContent).toBe(
    "No question could be asked: the reply offered no usable options.",
  );
  expect(within(row).queryAllByRole("button")).toHaveLength(0);
  expect(within(row).queryAllByRole("link")).toHaveLength(0);
  expect(within(row).queryByTestId("state-chip-from")).toBeNull();
  expect(screen.queryAllByRole("alert")).toHaveLength(0);
  // The one failure block is still the start failure, and it keeps the actions.
  const blocks = screen.getAllByTestId("thread-failure");
  expect(blocks).toHaveLength(1);
  expect(within(blocks[0]).getByTestId("thread-start-again")).toBeTruthy();
});

test("an unknown event kind renders a safe generic row", () => {
  const odd: MissionEvent = {
    ...failure(1),
    kind: "brand_new_kind",
    text: null,
    meta: { secret: "do not print" },
  };
  render(<Harness m={mission({ state: "running" })} events={[odd]} />);
  const row = screen.getByTestId("thread-event");
  // One bare line — its name and its time — rather than a box with a label over nothing (#1063).
  // The time comes from `event.at`; nothing comes from `meta`.
  expect(within(row).getByTestId("thread-system-bare").textContent).toMatch(/^Brand new kind/);
  expect(row.textContent).not.toContain("do not print");
  expect(row.textContent).not.toContain("secret");
});
