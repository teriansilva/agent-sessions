import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig, PulseConfig } from "../types/api";
import { PulseSettings } from "./PulseSettings";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
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

function renderPanel(b: PulseConfig | undefined = block(), refresh: () => void = () => {}) {
  const config = { csrf: "t", new_session_engines: [], terminal_backend: "ws", pulse: b } as AppConfig;
  return render(
    <ConfigRefreshCtx.Provider value={refresh}>
      <ConfigCtx.Provider value={config}>
        <PulseSettings />
      </ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>,
  );
}

beforeEach(() => {
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({ pulse: block({ auto_enabled: true }) });
  vi.mocked(api.pulseScan).mockReset();
});

test("toggling auto-scan persists pulse.auto_enabled (#441 P6)", async () => {
  renderPanel(block({ auto_enabled: false }));
  await userEvent.click(screen.getByRole("checkbox", { name: /scan automatically/i }));
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
  await userEvent.click(screen.getByRole("checkbox", { name: /scan automatically/i }));
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
  expect(api.setPrefs).toHaveBeenCalledWith({ pulse: { interval_minutes: 15 } });
});

test("changing the depth select persists pulse.scan_depth (#441 P6)", async () => {
  renderPanel(block({ scan_depth: "fast" }));
  await userEvent.selectOptions(screen.getByLabelText(/scan depth/i), "medium");
  expect(api.setPrefs).toHaveBeenCalledWith({ pulse: { scan_depth: "medium" } });
});

test("a non-fast depth with an unconfigured endpoint warns it degrades (#441 P6)", () => {
  renderPanel(block({ scan_depth: "medium", configured: false }));
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
    banner: null,
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
  expect(await screen.findByText(/curated 1 session.*synthesis skipped/i)).toBeInTheDocument();
});
