/** #1505: the API session's page opens on launch; the pane says what it is waiting for. */
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo, StructuredSnapshot } from "../../types/api";
import { StructuredPane } from "./StructuredPane";
import { handStart, resetStartHandoff } from "./startHandoff";
import { NEW_SESSION_PATH } from "../../lib/routes";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      structuredSnapshot: vi.fn(),
      structuredEvents: vi.fn(),
      structuredSubmit: vi.fn(),
      structuredStop: vi.fn(),
      structuredStart: vi.fn(),
      templates: vi.fn(),
      templateVariables: vi.fn(() => Promise.resolve({ variables: [], limits: {} })),
    },
  };
});

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `codex-api:${ID}`;
const REASON = "the claude CLI exited during setup (exit 1: not logged in)";

function snap(over: Partial<StructuredSnapshot> = {}): StructuredSnapshot {
  return {
    session_key: KEY,
    revision: 4,
    event_cursor: 4,
    cwd: "/home/u/proj",
    state: "idle",
    active_turn: null,
    model_requested: "gpt-5-codex",
    model_effective: null,
    turns: [],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: null, worker: "w1", background_active: false },
    read_only: null,
    ...over,
  };
}

const starting = (over: Partial<StructuredSnapshot> = {}) =>
  snap({ launch: { gen: "w1", state: "starting", since: 1 }, ...over });

function Where() {
  const location = useLocation();
  return <div data-testid="where">{JSON.stringify({ path: location.pathname, state: location.state })}</div>;
}

function renderPane() {
  return render(
    <MemoryRouter initialEntries={[`/s/codex-api/${ID}`]}>
      <Routes>
        <Route path="/s/:engine/:id" element={<StructuredPane engine="codex-api" id={ID} />} />
        <Route path={NEW_SESSION_PATH} element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

const sendButton = () => screen.getByTestId("structured-send");
// Start / Start again / Start (skips prompts) — never the Send button reading "Starting…".
const startButtons = () => screen.queryAllByRole("button", { name: /^start( again| \(|$)/i });

beforeEach(() => {
  setRoster(fixture.engines as EngineInfo[], []);
  // The journal cursor never moves in these tests: readiness is a lifecycle fact.
  vi.mocked(api.structuredEvents).mockResolvedValue({
    session_key: KEY,
    revision: 4,
    next_cursor: 4,
    events: [],
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  resetRoster();
  resetStartHandoff();
});

test("starting: Send waits (reads Starting…), the draft stays, and ready repaints within one poll", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  vi.mocked(api.structuredSnapshot)
    .mockResolvedValueOnce(starting())
    .mockResolvedValue(snap({ launch: { gen: "w1", state: "ready", since: 2 } }));
  renderPane();
  expect(await screen.findByTestId("structured-starting")).toHaveTextContent("Starting");
  expect(screen.getByTestId("structured-worker")).toHaveTextContent("starting");
  // The composer is usable: type a draft (nothing is queued, nothing is sent).
  const box = screen.getByRole("textbox", { name: /message/i });
  expect(box).toBeEnabled();
  await user.type(box, "draft while starting{Enter}");
  expect(sendButton()).toBeDisabled();
  expect(sendButton()).toHaveTextContent("Starting…");
  expect(screen.getByText("Type now — Send unlocks when the agent is ready.")).toBeInTheDocument();
  expect(api.structuredSubmit).not.toHaveBeenCalled();
  // Never a Start, and never "nothing has run", while a launched worker gets ready.
  expect(startButtons()).toHaveLength(0);
  expect(screen.queryByText(/nothing has run/i)).toBeNull();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_100); // ONE active tick, no cursor advance
  });
  await waitFor(() => expect(screen.queryByTestId("structured-starting")).toBeNull());
  expect(sendButton()).toHaveTextContent("Send");
  expect(sendButton()).toBeEnabled();
  // The draft typed meanwhile is still in the composer when Send enables (in memory, as ever).
  expect((box as HTMLTextAreaElement).value).toContain("draft while starting");
});

test("a skip start that fails repaints within one poll: its reason, Start again and Discard", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  vi.mocked(api.structuredSnapshot)
    .mockResolvedValueOnce(starting({ bypass: true, start_in_flight: true }))
    .mockResolvedValue(
      snap({
        bypass: true,
        pending_start: true,
        start_incomplete: true,
        start_failed: true,
        start_failed_reason: REASON,
        native: { native_id: null, worker: null, background_active: false },
        launch: { gen: "w1", state: "failed", reason: REASON, since: 2 },
      }),
    );
  vi.mocked(api.structuredStart).mockResolvedValue(starting({ bypass: true, start_in_flight: true }));
  renderPane();
  await screen.findByTestId("structured-starting");
  expect(startButtons()).toHaveLength(0);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_100);
  });
  const banner = await screen.findByTestId("structured-pending-start");
  expect(banner).toHaveTextContent(`Couldn’t start: ${REASON}`);
  expect(banner).not.toHaveTextContent(/nothing has run/i);
  expect(screen.getByTestId("structured-worker")).toHaveTextContent("start failed");
  expect(screen.getByRole("button", { name: "Discard" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "New session" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Start again" }));
  expect(api.structuredStart).toHaveBeenCalledWith(KEY);
});

test("a guarded create that failed offers New session and Discard — never Start again", async () => {
  const user = userEvent.setup();
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({
      native: { native_id: null, worker: null, background_active: false },
      launch: { gen: "w1", state: "failed", reason: REASON, since: 2 },
    }),
  );
  renderPane();
  const banner = await screen.findByTestId("structured-create-failed");
  expect(banner).toHaveTextContent(`Couldn’t start: ${REASON}`);
  expect(startButtons()).toHaveLength(0);
  expect(screen.getByRole("button", { name: "Discard" })).toBeEnabled();
  expect(screen.getByRole("textbox", { name: /message/i })).toBeDisabled();
  expect(sendButton()).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "New session" }));
  const where = JSON.parse((await screen.findByTestId("where")).textContent ?? "{}");
  expect(where.path).toBe(NEW_SESSION_PATH);
  // GUARDED again (Hermes on #1518, 4): a stored skip default must not flip the replacement.
  expect(where.state.apiGuardedEngine).toBe("codex-api");
  // Back to New session with the same agent, folder and model preselected.
  expect(where.state.restoreDraft).toMatchObject({
    engineChoice: "codex-api",
    cwdOverride: "/home/u/proj",
    modelChoice: { engine: "codex-api", model: "gpt-5-codex" },
  });
});

test("a start refused before any worker existed shows its reason at once, with Start again", async () => {
  const user = userEvent.setup();
  let started = false;
  vi.mocked(api.structuredSnapshot).mockImplementation(async () =>
    started
      ? starting({ bypass: true, start_in_flight: true })
      : snap({
          bypass: true,
          pending_start: true,
          native: { native_id: null, worker: null, background_active: false },
          launch: { gen: null, state: "failed", reason: "the CLI went away", since: 1 },
        }),
  );
  vi.mocked(api.structuredStart).mockImplementation(async () => {
    started = true;
    return starting({ bypass: true, start_in_flight: true });
  });
  handStart(KEY, Promise.reject(new ApiError(409, "the CLI went away")));
  renderPane();
  const banner = await screen.findByTestId("structured-pending-start");
  await waitFor(() => expect(banner).toHaveTextContent("Couldn’t start: the CLI went away"));
  expect(screen.queryByTestId("structured-starting")).toBeNull();
  await user.click(screen.getByRole("button", { name: "Start again" }));
  expect(api.structuredStart).toHaveBeenCalledTimes(1);
  expect(await screen.findByTestId("structured-starting")).toBeInTheDocument();
});

test("a lost start answer says it may not have reached BattleLab; Check again re-reads", async () => {
  const user = userEvent.setup();
  vi.mocked(api.structuredSnapshot)
    .mockResolvedValueOnce(
      snap({ bypass: true, pending_start: true, native: { native_id: null, worker: null, background_active: false } }),
    )
    // It did reach the server after all: the re-read shows the launch.
    .mockResolvedValue(starting({ bypass: true, start_in_flight: true }));
  handStart(KEY, Promise.reject(new TypeError("Failed to fetch")));
  renderPane();
  expect(await screen.findByTestId("structured-start-unknown")).toHaveTextContent(
    "Start may not have reached BattleLab",
  );
  expect(screen.queryByTestId("structured-starting")).toBeNull(); // no answer, no launch view
  expect(screen.getByRole("button", { name: "Start again" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "Check again" }));
  expect(api.structuredStart).not.toHaveBeenCalled(); // Check again only reads
  expect(await screen.findByTestId("structured-starting")).toBeInTheDocument();
  expect(screen.queryByTestId("structured-start-unknown")).toBeNull();
});

test("with no launch view and no answer it is the pending Start/Discard, never Starting…", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({ bypass: true, pending_start: true, native: { native_id: null, worker: null, background_active: false } }),
  );
  renderPane();
  const banner = await screen.findByTestId("structured-pending-start");
  expect(banner).toHaveTextContent("hasn’t started");
  expect(screen.getByRole("button", { name: "Start (skips prompts)" })).toBeEnabled();
  expect(screen.queryByTestId("structured-starting")).toBeNull();
});

test("a handed start that answered shows its launch at once", async () => {
  let answer: (s: StructuredSnapshot) => void = () => {};
  let launched = false; // the server: pending until the start launched, starting after
  vi.mocked(api.structuredSnapshot).mockImplementation(async () =>
    launched
      ? starting({ bypass: true, start_in_flight: true, revision: 5, event_cursor: 5 })
      : snap({ bypass: true, pending_start: true, native: { native_id: null, worker: null, background_active: false } }),
  );
  handStart(
    KEY,
    new Promise<StructuredSnapshot>((resolve) => (answer = resolve)).then((s) => {
      launched = true;
      return s;
    }),
  );
  renderPane();
  const banner = await screen.findByTestId("structured-pending-start");
  // In flight: no second Start can be pressed meanwhile.
  expect(screen.getByRole("button", { name: "Start (skips prompts)" })).toBeDisabled();
  expect(banner).toBeInTheDocument();
  await act(async () => answer(starting({ bypass: true, start_in_flight: true, revision: 5, event_cursor: 5 })));
  expect(await screen.findByTestId("structured-starting")).toBeInTheDocument();
  expect(screen.queryByTestId("structured-pending-start")).toBeNull();
});

test("while a start is in flight the snapshot is read every tick: Starting… shows within one poll", async () => {
  // Hermes on #1518 (6): the handoff used to read only the event cursor (unchanged by a launch)
  // and re-read the snapshot on every 5th tick, so Starting… could take ~5 s to appear.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.structuredSnapshot)
    .mockResolvedValueOnce(
      snap({ bypass: true, pending_start: true, native: { native_id: null, worker: null, background_active: false } }),
    )
    .mockResolvedValue(starting({ bypass: true, start_in_flight: true }));
  handStart(KEY, new Promise<StructuredSnapshot>(() => {})); // its answer is slow
  renderPane();
  await screen.findByTestId("structured-pending-start");
  expect(screen.queryByTestId("structured-starting")).toBeNull();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_100); // ONE tick
  });
  expect(await screen.findByTestId("structured-starting")).toBeInTheDocument();
});

test("a late /start answer never takes a ready session back to Starting… (same revision)", async () => {
  // Hermes on #1518 (5): a poll read `ready` before the delayed `/start` response (`starting`)
  // arrived, both at journal revision 4.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let answer: (s: StructuredSnapshot) => void = () => {};
  vi.mocked(api.structuredSnapshot)
    .mockResolvedValueOnce(
      snap({ bypass: true, pending_start: true, native: { native_id: null, worker: null, background_active: false } }),
    )
    .mockResolvedValue(snap({ bypass: true, launch: { gen: "w1", state: "ready", since: 2 } }));
  handStart(KEY, new Promise<StructuredSnapshot>((resolve) => (answer = resolve)));
  renderPane();
  await screen.findByTestId("structured-pending-start");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_100);
  });
  await waitFor(() => expect(sendButton()).toHaveTextContent("Send"));
  await act(async () => answer(starting({ bypass: true, start_in_flight: true })));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(50);
  });
  expect(screen.queryByTestId("structured-starting")).toBeNull();
  expect(sendButton()).toHaveTextContent("Send");
  expect(screen.getByTestId("structured-worker")).not.toHaveTextContent("starting");
});

test("a late read of the failed generation never replaces its Start again (same revision)", async () => {
  // Hermes on #1518 (round 2, 3): A failed; Start again launched B; a read of A arriving after B
  // used to put the failure back.
  const user = userEvent.setup();
  const failedA = snap({
    bypass: true,
    pending_start: true,
    start_incomplete: true,
    start_failed: true,
    start_failed_reason: REASON,
    native: { native_id: null, worker: null, background_active: false },
    launch: { gen: "A", state: "failed", reason: REASON, since: 100 },
  });
  // Every read here is the late one: it still says A failed.
  vi.mocked(api.structuredSnapshot).mockResolvedValue(failedA);
  vi.mocked(api.structuredStart).mockResolvedValue(
    snap({ bypass: true, start_in_flight: true, launch: { gen: "B", state: "starting", since: 200 } }),
  );
  renderPane();
  await waitFor(() => expect(screen.getByTestId("structured-pending-start")).toHaveTextContent("Couldn’t start"));
  await user.click(screen.getByRole("button", { name: "Start again" }));
  expect(api.structuredStart).toHaveBeenCalledTimes(1);
  // The start's own answer (B starting) is shown; the late read of A that follows is rejected.
  await waitFor(() => expect(vi.mocked(api.structuredSnapshot).mock.calls.length).toBeGreaterThanOrEqual(2));
  expect(await screen.findByTestId("structured-starting")).toBeInTheDocument();
  expect(screen.queryByTestId("structured-pending-start")).toBeNull();
});

test("a pre-start read that resolves late never erases a failed skip start; Discard still clears it", async () => {
  // Hermes on #1518 (round 3): the initial GET (pending, no launch view) was still in flight when
  // the handed `/start` answered and the poll showed the failure; resolving it last, at the same
  // revision, replaced the failure and its reason.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const pending = snap({
    bypass: true,
    pending_start: true,
    native: { native_id: null, worker: null, background_active: false },
  });
  const failed = snap({
    bypass: true,
    pending_start: true,
    start_incomplete: true,
    start_failed: true,
    start_failed_reason: REASON,
    native: { native_id: null, worker: null, background_active: false },
    launch: { gen: "worker-A", state: "failed", reason: REASON, since: 200 },
  });
  const discarded = snap({ bypass: true, native: { native_id: null, worker: null, background_active: false } });
  let initial: (s: StructuredSnapshot) => void = () => {};
  let discardedNow = false;
  vi.mocked(api.structuredSnapshot)
    .mockImplementationOnce(() => new Promise<StructuredSnapshot>((resolve) => (initial = resolve)))
    .mockImplementation(async () => (discardedNow ? discarded : failed));
  vi.mocked(api.structuredStop).mockImplementation(async () => {
    discardedNow = true;
    return { containment: "gone" };
  });
  handStart(
    KEY,
    Promise.resolve(snap({ bypass: true, start_in_flight: true, launch: { gen: "worker-A", state: "starting", since: 100 } })),
  );
  renderPane();
  expect(await screen.findByTestId("structured-starting")).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_100); // one fast poll: the waiter recorded the failure
  });
  const banner = await screen.findByTestId("structured-pending-start");
  expect(banner).toHaveTextContent(`Couldn’t start: ${REASON}`);
  await act(async () => initial(pending)); // the pre-start read finally answers
  await act(async () => {
    await vi.advanceTimersByTimeAsync(50);
  });
  expect(screen.getByTestId("structured-pending-start")).toHaveTextContent(`Couldn’t start: ${REASON}`);
  expect(screen.getByTestId("structured-worker")).toHaveTextContent("start failed");
  expect(screen.getByRole("button", { name: "Start again" })).toBeEnabled();
  // The view keeps being read (its own cadence) and keeps saying so.
  const reads = vi.mocked(api.structuredSnapshot).mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_500);
  });
  expect(vi.mocked(api.structuredSnapshot).mock.calls.length).toBeGreaterThan(reads);
  expect(screen.getByTestId("structured-pending-start")).toHaveTextContent(`Couldn’t start: ${REASON}`);
  // Discard is the legitimate way out: its snapshot is not pending, so the view clears.
  await user.click(screen.getByRole("button", { name: "Discard" }));
  expect(api.structuredStop).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(screen.queryByTestId("structured-pending-start")).toBeNull());
  expect(screen.queryByText(/Couldn’t start/)).toBeNull();
});

const FAILED_A = snap({
  bypass: true,
  pending_start: true,
  start_incomplete: true,
  start_failed: true,
  start_failed_reason: REASON,
  native: { native_id: null, worker: null, background_active: false },
  launch: { gen: "worker-A", state: "failed", reason: REASON, since: 200 },
});

test("a read sent before Discard and answered after it never brings the failure back (sequence fence)", async () => {
  // Hermes on #1518 (round 4): after Discard the view has no launch and is not pending; a GET
  // sent just before Discard answered with the old `failed`, at the same revision, and the
  // per-case checks let it back in.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const discarded = snap({ bypass: true, native: { native_id: null, worker: null, background_active: false } });
  let late: (s: StructuredSnapshot) => void = () => {};
  vi.mocked(api.structuredSnapshot)
    .mockResolvedValueOnce(FAILED_A) // first read: the failure
    .mockImplementationOnce(() => new Promise<StructuredSnapshot>((resolve) => (late = resolve)))
    .mockResolvedValue(discarded); // every read after the Discard
  vi.mocked(api.structuredStop).mockResolvedValue({ containment: "gone" });
  renderPane();
  await waitFor(() => expect(screen.getByTestId("structured-pending-start")).toHaveTextContent(REASON));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_500); // the idle poll sends its read … still in flight
  });
  expect(api.structuredSnapshot).toHaveBeenCalledTimes(2);
  await user.click(screen.getByRole("button", { name: "Discard" }));
  await waitFor(() => expect(screen.queryByTestId("structured-pending-start")).toBeNull());
  await act(async () => late(FAILED_A)); // the pre-Discard read answers last
  await act(async () => {
    await vi.advanceTimersByTimeAsync(50);
  });
  expect(screen.queryByTestId("structured-pending-start")).toBeNull();
  expect(screen.queryByText(/Couldn’t start/)).toBeNull();
  expect(screen.queryByRole("button", { name: "Start again" })).toBeNull();
  // Reads sent after the Discard still apply: the fence orders, it does not freeze.
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ ...discarded, revision: 6, event_cursor: 6, model_requested: "later" }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_500);
  });
  await waitFor(() => expect(screen.getByTestId("structured-model")).toHaveTextContent("later"));
});

test("Start again after a failure: a read sent before it never overwrites its views, later reads do", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const pendingNoView = snap({
    bypass: true,
    pending_start: true,
    native: { native_id: null, worker: null, background_active: false },
  });
  let late: (s: StructuredSnapshot) => void = () => {};
  let launched = false;
  vi.mocked(api.structuredSnapshot)
    .mockResolvedValueOnce(FAILED_A)
    .mockImplementationOnce(() => new Promise<StructuredSnapshot>((resolve) => (late = resolve)))
    .mockImplementation(async () =>
      launched
        ? snap({ bypass: true, launch: { gen: "worker-B", state: "ready", since: 300 } })
        : FAILED_A,
    );
  vi.mocked(api.structuredStart).mockImplementation(async () => {
    launched = true;
    return snap({ bypass: true, start_in_flight: true, launch: { gen: "worker-B", state: "starting", since: 250 } });
  });
  renderPane();
  await waitFor(() => expect(screen.getByTestId("structured-pending-start")).toHaveTextContent(REASON));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_500); // a read goes out before Start again …
  });
  await user.click(screen.getByRole("button", { name: "Start again" }));
  await act(async () => late(pendingNoView)); // … and answers after it
  await act(async () => {
    await vi.advanceTimersByTimeAsync(50);
  });
  expect(screen.queryByTestId("structured-pending-start")).toBeNull();
  // The start's own view, then the next read (B ready), both apply.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_100);
  });
  await waitFor(() => expect(sendButton()).toHaveTextContent("Send"));
  expect(screen.queryByTestId("structured-starting")).toBeNull();
  expect(screen.queryByTestId("structured-pending-start")).toBeNull();
});
