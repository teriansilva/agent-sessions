/** The Updates card's progress panel (#1085): step, bar, elapsed, through the restart gap. */
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { UpdateProgress } from "../../types/api";

import { PROGRESS_POLL_MS, UpdateProgressPanel } from "./UpdateProgressPanel";

vi.mock("../../lib/api", () => ({ api: { updateProgress: vi.fn() } }));

const NOW = 1_800_000_000;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW * 1000);
  vi.mocked(api.updateProgress).mockReset();
});
afterEach(() => vi.useRealTimers());

const running = (step: number, label: string): UpdateProgress => ({
  state: "running",
  steps: 7,
  step: "x",
  step_index: step,
  label,
  started_at: NOW - 161,
  elapsed_s: 161,
  last_duration_s: 360,
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

test("idle shows nothing but the last duration, when one is known", async () => {
  vi.mocked(api.updateProgress).mockResolvedValue({
    state: "idle",
    steps: 7,
    last_duration_s: 300,
  });
  render(<UpdateProgressPanel started={0} />);
  await flush();
  expect(screen.queryByTestId("update-progress")).toBeNull();
  expect(screen.getByTestId("update-last-duration")).toHaveTextContent("5 min");
});

test("a run in flight shows the step, the bar and the time, and keeps polling", async () => {
  vi.mocked(api.updateProgress).mockResolvedValue(running(4, "Building the web UI"));
  render(<UpdateProgressPanel started={0} />);
  await flush();
  const panel = screen.getByTestId("update-progress");
  expect(panel).toHaveTextContent("Step 4 of 7 · Building the web UI");
  expect(panel).toHaveTextContent("2:41 elapsed · last update took 6 min");
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "4");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(PROGRESS_POLL_MS);
  });
  expect(api.updateProgress).toHaveBeenCalledTimes(2);
});

test("a failed read mid-update is the restart, not an error — and the new process finishes it", async () => {
  vi.mocked(api.updateProgress)
    .mockResolvedValueOnce(running(6, "Restarting the service"))
    .mockRejectedValueOnce(new Error("502"))
    .mockResolvedValueOnce({
      state: "done",
      steps: 7,
      step: "health",
      step_index: 7,
      label: "Checking it came back",
      started_at: NOW - 300,
      elapsed_s: 290,
      last_duration_s: 290,
    });
  render(<UpdateProgressPanel started={1} />);
  await flush();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(PROGRESS_POLL_MS);
  });
  expect(screen.getByTestId("update-progress")).toHaveTextContent("Restarting… reconnecting");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(PROGRESS_POLL_MS);
  });
  expect(screen.getByTestId("update-progress")).toHaveTextContent("Update finished");
  expect(screen.getByTestId("update-reload")).toBeInTheDocument();
  // Terminal: the poll stops.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(PROGRESS_POLL_MS * 3);
  });
  expect(api.updateProgress).toHaveBeenCalledTimes(3);
});

test("a rollback says so, in the server's own step label", async () => {
  vi.mocked(api.updateProgress).mockResolvedValue({
    ...running(7, "Checking it came back"),
    state: "rolled_back",
    elapsed_s: 400,
  });
  render(<UpdateProgressPanel started={1} />);
  await flush();
  expect(screen.getByTestId("update-progress")).toHaveTextContent(
    'Update failed at "Checking it came back" — rolled back to the previous release',
  );
});

test("an old finished run is history, not shown when the card opens", async () => {
  vi.mocked(api.updateProgress).mockResolvedValue({
    state: "done",
    steps: 7,
    step_index: 7,
    label: "Checking it came back",
    started_at: NOW - 86_400,
    elapsed_s: 300,
    last_duration_s: 300,
  });
  render(<UpdateProgressPanel started={0} />);
  await flush();
  expect(screen.queryByTestId("update-progress")).toBeNull();
  // …but how long it took is still said.
  expect(screen.getByTestId("update-last-duration")).toHaveTextContent("5 min");
});

test("a run that finished before this page loaded is shown, but offers no Reload", async () => {
  // Hermes on #1085: a stale `done` must not ask for a reload the page already had.
  vi.setSystemTime(performance.timeOrigin + 60_000);
  vi.mocked(api.updateProgress).mockResolvedValue({
    state: "done",
    steps: 7,
    step_index: 7,
    label: "Checking it came back",
    started_at: performance.timeOrigin / 1000 - 600,
    elapsed_s: 300,
    last_duration_s: 300,
  });
  render(<UpdateProgressPanel started={0} />);
  await flush();
  expect(screen.getByTestId("update-progress")).toHaveTextContent("Update finished");
  expect(screen.queryByTestId("update-reload")).toBeNull();
});

test("opened during the restart gap: a failed FIRST read is retried until the server answers (#1089)", async () => {
  // Hermes on #1089: the first read failing (the server is restarting) used to end discovery.
  vi.mocked(api.updateProgress)
    .mockRejectedValueOnce(new Error("502"))
    .mockRejectedValueOnce(new Error("502"))
    .mockResolvedValue(running(7, "Checking it came back"));
  render(<UpdateProgressPanel started={0} />);
  await flush();
  expect(screen.queryByTestId("update-progress")).toBeNull();
  // Backoff: 2 s, then 4 s.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(PROGRESS_POLL_MS);
  });
  expect(api.updateProgress).toHaveBeenCalledTimes(2);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(PROGRESS_POLL_MS * 2);
  });
  expect(api.updateProgress).toHaveBeenCalledTimes(3);
  expect(screen.getByTestId("update-progress")).toHaveTextContent(
    "Step 7 of 7 · Checking it came back",
  );
});

test("the backoff before any run is seen is bounded", async () => {
  vi.mocked(api.updateProgress).mockRejectedValue(new Error("down"));
  render(<UpdateProgressPanel started={0} />);
  await flush();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10 * 60_000);
  });
  // ≤ one read per PROGRESS_RETRY_MAX_MS once the backoff caps, and it never stops asking.
  const n = vi.mocked(api.updateProgress).mock.calls.length;
  expect(n).toBeGreaterThan(10);
  expect(n).toBeLessThan(40);
});
