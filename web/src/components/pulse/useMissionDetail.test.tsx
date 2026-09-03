/** The mission loader's CADENCES (#890, #902 review 2).
 *
 *  The detail poll exists for the supervisor's reading, which changes underneath a console nobody
 *  is touching — and at 150s it is the right cadence for a board. It is the wrong cadence for a
 *  CONVERSATION: a model call takes seconds, and on a fresh page there is no outstanding composer
 *  request whose callback could ask again, so a reloaded `in_progress` turn sat saying "Still
 *  working" and withheld an answer the server already had for almost two and a half minutes.
 *
 *  So an open turn gets its own short, bounded cadence, and it stops the moment the turn settles.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";

import { useMissionDetail } from "./useMissionDetail";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      mission: vi.fn(),
      missionObjectives: vi.fn(),
      missionContext: vi.fn(),
    },
  };
});

const CTX = {
  id: "msn_1",
  project_id: "",
  cwd: "/repo",
  sessions: [],
  git: null,
  git_error: null,
};

const RUNNING_TURN = {
  turn_id: "t1",
  state: "in_progress",
  text: "run the tests",
  delivery_error: "",
  created_at: 1,
};

function detail(turn: unknown) {
  return {
    id: "msn_1",
    state: "running",
    sessions: [],
    events: [],
    events_next_seq: null,
    turn,
  };
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.missionContext).mockResolvedValue(CTX as never);
  vi.mocked(api.missionObjectives).mockResolvedValue({ objectives: [] });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

test("an OPEN TURN is polled on its own short cadence, and the poll stops when it settles", async () => {
  let turn: unknown = RUNNING_TURN;
  vi.mocked(api.mission).mockImplementation(async () => detail(turn) as never);

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(result.current.mission?.turn).toBeTruthy());
  const afterMount = vi.mocked(api.mission).mock.calls.length;

  // Well inside the 150s detail cadence — on that alone nothing would have been re-read.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(vi.mocked(api.mission).mock.calls.length).toBeGreaterThan(afterMount);

  // It settles, and the short cadence stops: a console with nothing in flight is exactly as
  // quiet as it was before.
  turn = null;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  await waitFor(() => expect(result.current.mission?.turn).toBeNull());
  const settled = vi.mocked(api.mission).mock.calls.length;

  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });
  expect(vi.mocked(api.mission).mock.calls.length).toBe(settled);
});

test("the open-turn poll is BOUNDED, so a turn that never settles cannot poll for ever", async () => {
  // A turn that stays open is a real state — a worker can die mid-turn and recovery re-drives it
  // — and an unbounded 3s cadence would hit the endpoint for the life of the page.
  vi.mocked(api.mission).mockImplementation(
    async () => detail(RUNNING_TURN) as never,
  );
  renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.mission).toHaveBeenCalled());

  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_000 * 70);
  });
  const capped = vi.mocked(api.mission).mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_000 * 20);
  });
  // Only the ordinary 150s cadence is left running.
  expect(vi.mocked(api.mission).mock.calls.length - capped).toBeLessThanOrEqual(
    1,
  );
});

test("a console with NO open turn keeps the ordinary cadence and nothing else", async () => {
  // The mirror. The short poll is for a conversation in flight; a console watching a board must
  // not pay for it.
  vi.mocked(api.mission).mockImplementation(async () => detail(null) as never);
  renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.mission).toHaveBeenCalled());
  const afterMount = vi.mocked(api.mission).mock.calls.length;

  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
  expect(vi.mocked(api.mission).mock.calls.length).toBe(afterMount);
});
