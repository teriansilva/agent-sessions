import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig, PulseConfig } from "../types/api";
import { PulseSettings } from "./PulseSettings";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { setPrefs: vi.fn() } };
});

function block(over: Partial<PulseConfig> = {}): PulseConfig {
  return { window_days: 3, configured: true, ...over };
}

function renderPanel(
  b: PulseConfig | undefined = block(),
  refresh: () => void = () => {},
) {
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    pulse: b,
  } as AppConfig;
  return render(
    <ConfigRefreshCtx.Provider value={refresh}>
      <ConfigCtx.Provider value={config}>
        <PulseSettings />
      </ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>,
  );
}

beforeEach(() => {
  vi.mocked(api.setPrefs)
    .mockReset()
    .mockResolvedValue({ pulse: block({ window_days: 2 }) });
});

/** Type `value` into the window and leave the field — the one save the card makes. */
function commit(value: string) {
  const input = screen.getByLabelText("How far back to look");
  fireEvent.change(input, { target: { value } });
  fireEvent.blur(input);
}

test("the card is the recent window only: the retired scan controls are gone (#2179)", () => {
  renderPanel();
  expect(screen.getByRole("heading", { name: "Recent window" })).toHaveAttribute("id", "pulse-h");
  expect(screen.getByLabelText("How far back to look")).toHaveValue(3);
  for (const gone of [/scan now/i, /scan automatically/i, /scan every/i, /scan depth/i]) {
    expect(screen.queryByText(gone)).toBeNull();
  }
  expect(screen.queryByRole("button")).toBeNull();
  expect(screen.queryByRole("checkbox")).toBeNull();
});

test("the window commits on blur within bounds; out-of-range reverts (#441 P6, #1086)", async () => {
  renderPanel(block({ window_days: 3 }));
  const input = screen.getByLabelText("How far back to look");
  await userEvent.clear(input);
  await userEvent.type(input, "2");
  await userEvent.tab();
  expect(api.setPrefs).toHaveBeenCalledWith({ pulse: { window_days: 2 } });

  vi.mocked(api.setPrefs).mockClear();
  await userEvent.clear(input);
  await userEvent.type(input, "7"); // valid under the old 1–30 range, above the 3-day ceiling now
  await userEvent.tab();
  expect(api.setPrefs).not.toHaveBeenCalled();
  expect(input).toHaveValue(2); // back to the value the server accepted above
  // …and the revert says why instead of silently snapping back.
  expect(screen.getByText(/between 1 and 3 days/i)).toBeInTheDocument();
});

test("a successful save flashes a Saved note and refreshes the config context", async () => {
  const refresh = vi.fn();
  renderPanel(block({ window_days: 3 }), refresh);
  commit("2");
  expect(await screen.findByText("Saved.")).toBeInTheDocument();
  // Without the refresh, ConfigCtx keeps the app-load values and a remount of the panel
  // (switching Settings tabs and back) would show the pre-save state as if the save was lost.
  expect(refresh).toHaveBeenCalled();
});

test("Enter commits the window the same way blur does", async () => {
  renderPanel(block({ window_days: 3 }));
  const input = screen.getByLabelText("How far back to look");
  await userEvent.clear(input);
  await userEvent.type(input, "1{Enter}");
  expect(api.setPrefs).toHaveBeenCalledWith({ pulse: { window_days: 1 } });
});

test("the saved-toast timer does not outlive the panel (#922)", async () => {
  // WHY THIS ASSERTS ON THE TIMER AND NOT ON THE SYMPTOM.
  //
  // The CI failure is `ReferenceError: window is not defined`, raised when the pending
  // `setSaved(false)` runs after the jsdom ENVIRONMENT is torn down — which happens at the end
  // of a test file, not at the end of a test. One case cannot reproduce that, and a test that
  // waited on wall-clock would be green here for the same reason the bug is intermittent in CI:
  // the teardown usually wins the race.
  //
  // So it asserts the property the fix actually establishes, in the domain the fix lives in:
  // after unmount, this component has no pending timer. Fake timers make that exact and
  // instantaneous rather than probabilistic.
  vi.useFakeTimers();
  try {
    const { unmount } = renderPanel(block({ window_days: 3 }));
    // `fireEvent`, not `userEvent`: the latter schedules its own delays and `findByText` polls
    // on REAL time, so both hang forever against a fake clock. This test needs one synchronous
    // commit and an explicit flush of the awaited `setPrefs`, which is exactly what these give.
    await act(async () => {
      commit("2");
      await Promise.resolve();
    });
    expect(screen.getByText("Saved.")).toBeInTheDocument();

    // The 1500ms toast timer is now in flight — the precondition, asserted so this test fails
    // loudly if it ever stops exercising the path it names.
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    unmount();
    expect(vi.getTimerCount()).toBe(0);
  } finally {
    vi.useRealTimers();
  }
});

test("a save resolving AFTER unmount does not arm a new timer (#922 review 1)", async () => {
  // The half the first fix missed. Cleanup cancels a timer that already EXISTS, and `save()`
  // awaits `setPrefs` before creating one — so leaving the panel mid-request means the cleanup
  // runs against an empty ref and the continuation then installs a fresh 1500 ms timeout on a
  // component that is gone. The leak survived its own fix.
  //
  // The earlier regression cannot catch this: it resolves the save BEFORE unmounting, so it
  // only ever covers an already-armed timer.
  vi.useFakeTimers();
  try {
    let release!: (v: unknown) => void;
    vi.mocked(api.setPrefs).mockReturnValue(
      new Promise((r) => {
        release = r;
      }) as ReturnType<typeof api.setPrefs>,
    );

    const { unmount } = renderPanel(block({ window_days: 3 }));
    await act(async () => {
      commit("2");
      await Promise.resolve();
    });
    // Precondition: the request is still in flight, so nothing is armed yet — which is exactly
    // why unmount cleanup has nothing to cancel.
    expect(vi.getTimerCount()).toBe(0);

    unmount();
    await act(async () => {
      release({ pulse: block({ window_days: 2 }) });
      await Promise.resolve();
    });

    expect(vi.getTimerCount()).toBe(0);
    // …and nothing lands late either, which is the failure the operator actually sees.
    vi.advanceTimersByTime(3000);
    expect(vi.getTimerCount()).toBe(0);
  } finally {
    vi.useRealTimers();
  }
});
