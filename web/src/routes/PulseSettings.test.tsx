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
  return { ...actual, api: { setPrefs: vi.fn(), pulseScan: vi.fn() } };
});

function block(over: Partial<PulseConfig> = {}): PulseConfig {
  return {
    auto_enabled: false,
    interval_minutes: 30,
    window_days: 3,
    scan_depth: "fast",
    configured: true,
    ...over,
  };
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
    .mockResolvedValue({ pulse: block({ auto_enabled: true }) });
  vi.mocked(api.pulseScan).mockReset();
});

test("toggling auto-scan persists pulse.auto_enabled (#441 P6)", async () => {
  renderPanel(block({ auto_enabled: false }));
  await userEvent.click(
    screen.getByRole("checkbox", { name: /scan automatically/i }),
  );
  expect(api.setPrefs).toHaveBeenCalledWith({ pulse: { auto_enabled: true } });
});

test("the window commits on blur within bounds; out-of-range reverts (#441 P6)", async () => {
  renderPanel(block({ window_days: 3 }));
  const input = screen.getByLabelText(/recent window/i);
  await userEvent.clear(input);
  await userEvent.type(input, "7");
  await userEvent.tab();
  expect(api.setPrefs).toHaveBeenCalledWith({ pulse: { window_days: 7 } });

  vi.mocked(api.setPrefs).mockClear();
  await userEvent.clear(input);
  await userEvent.type(input, "99"); // above the 30-day ceiling → revert, no save
  await userEvent.tab();
  expect(api.setPrefs).not.toHaveBeenCalled();
  expect(input).toHaveValue(3);
  // …and the revert says why instead of silently snapping back.
  expect(screen.getByText(/between 1 and 30 days/i)).toBeInTheDocument();
});

test("a successful save flashes a Saved note and refreshes the config context", async () => {
  const refresh = vi.fn();
  renderPanel(block({ auto_enabled: false }), refresh);
  await userEvent.click(
    screen.getByRole("checkbox", { name: /scan automatically/i }),
  );
  expect(await screen.findByText("Saved.")).toBeInTheDocument();
  // Without the refresh, ConfigCtx keeps the app-load values and a remount of the panel
  // (switching Settings tabs and back) would show the pre-save state as if the save was lost.
  expect(refresh).toHaveBeenCalled();
});

test("Enter commits a number field the same way blur does", async () => {
  renderPanel(block({ interval_minutes: 30 }));
  const input = screen.getByLabelText(/scan every/i);
  await userEvent.clear(input);
  await userEvent.type(input, "15{Enter}");
  expect(api.setPrefs).toHaveBeenCalledWith({
    pulse: { interval_minutes: 15 },
  });
});

test("changing the depth select persists pulse.scan_depth (#441 P6)", async () => {
  renderPanel(block({ scan_depth: "fast" }));
  await userEvent.selectOptions(screen.getByLabelText(/scan depth/i), "slow");
  expect(api.setPrefs).toHaveBeenCalledWith({
    pulse: { scan_depth: "slow" },
  });
});

test("the depth offers Fast and Slow only — Medium is gone (#956)", () => {
  renderPanel(block());
  const options = Array.from(
    (screen.getByLabelText(/scan depth/i) as HTMLSelectElement).options,
  ).map((o) => o.value);
  expect(options).toEqual(["fast", "slow"]);
});

test("a non-fast depth with an unconfigured endpoint warns it degrades (#441 P6)", () => {
  renderPanel(block({ scan_depth: "slow", configured: false }));
  expect(screen.getByText(/degrade to fast curation/i)).toBeInTheDocument();
});

test("Scan now reports the curated count + a degraded scan (#441 P6)", async () => {
  vi.mocked(api.pulseScan).mockResolvedValue({
    cache_version: 1,
    generated_at: 1,
    window_days: 3,
    scan_depth: "fast",
    input_fingerprint: "fp",
    synthesis_skipped: true,
    cards: [
      {
        id: "claude:a",
        engine: "claude",
        title: "t",
        cwd: "/x",
        project: { kind: "folder", id: "/x", name: "x" },
        last_activity: 1,
        ai_summary: "",
        intervention_required: false,
        intervention_reason: "",
        reviewed_at: null,
        live: false,
        state: "idle",
        synthesis: null,
      },
    ],
  });
  renderPanel(block());
  await userEvent.click(screen.getByRole("button", { name: /scan now/i }));
  expect(api.pulseScan).toHaveBeenCalledWith({ depth: "fast" });
  expect(
    await screen.findByText(/curated 1 session.*synthesis skipped/i),
  ).toBeInTheDocument();
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
    const { unmount } = renderPanel(block({ auto_enabled: false }));
    // `fireEvent`, not `userEvent`: the latter schedules its own delays and `findByText` polls
    // on REAL time, so both hang forever against a fake clock. This test needs one synchronous
    // click and an explicit flush of the awaited `setPrefs`, which is exactly what these give.
    await act(async () => {
      fireEvent.click(screen.getByRole("checkbox", { name: /scan automatically/i }));
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

    const { unmount } = renderPanel(block({ auto_enabled: false }));
    await act(async () => {
      fireEvent.click(screen.getByRole("checkbox", { name: /scan automatically/i }));
      await Promise.resolve();
    });
    // Precondition: the request is still in flight, so nothing is armed yet — which is exactly
    // why unmount cleanup has nothing to cancel.
    expect(vi.getTimerCount()).toBe(0);

    unmount();
    await act(async () => {
      release({ pulse: block({ auto_enabled: true }) });
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
