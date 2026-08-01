/** The idle window is an operator setting (#768).
 *
 * It replaced a hard-coded 48h constant. Measured on a live store, the median session was
 * 30.4h idle when the orchestrator escalated it — so 48h removed 18% of the notification
 * volume where the 24h default removes 52%. What the panel has to get right is that the
 * chosen value actually reaches the server, and that remounting shows the saved value rather
 * than the pre-save one (the #667 stale-ConfigCtx failure).
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig, OrchestratorConfig } from "../types/api";
import { OrchestratorSettings } from "./OrchestratorSettings";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { setPrefs: vi.fn() } };
});

vi.mock("../components/pulse/PushDevices", () => ({
  PushDevices: () => null,
}));

function block(over: Partial<OrchestratorConfig> = {}): OrchestratorConfig {
  return {
    enabled: true,
    autonomy: "suggest",
    allowed_verbs: ["continue"],
    auto_verbs_ceiling: ["continue"],
    confidence_min: 0.75,
    interval_minutes: 10,
    max_actions_per_pass: 4,
    proposal_ttl_minutes: 30,
    stale_hours: 24,
    nudge_template: "Please continue.",
    prompt: "p",
    notify: "escalations",
    configured: true,
    default_prompt: "p",
    default_nudge_template: "Please continue.",
    ...over,
  };
}

function renderPanel(b = block(), refresh: () => void = () => {}) {
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    orchestrator: b,
  } as unknown as AppConfig;
  return render(
    <ConfigRefreshCtx.Provider value={refresh}>
      <ConfigCtx.Provider value={config}>
        <OrchestratorSettings />
      </ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>,
  );
}

beforeEach(() => {
  vi.mocked(api.setPrefs)
    .mockReset()
    .mockResolvedValue({ orchestrator: block({ stale_hours: 6 }) });
});

test("the window shows the stored value and saves the chosen one as a number", async () => {
  renderPanel(block({ stale_hours: 24 }));
  const select = screen.getByLabelText(/idle for/i);
  expect(select).toHaveValue("24");

  await userEvent.selectOptions(select, "6");
  // A number, not the option's string value — the server rejects a string outright.
  expect(api.setPrefs).toHaveBeenCalledWith({
    orchestrator: { stale_hours: 6 },
  });
});

test("it refreshes the shared config so a remount does not show the pre-save value", async () => {
  const refresh = vi.fn();
  renderPanel(block({ stale_hours: 24 }), refresh);
  await userEvent.selectOptions(screen.getByLabelText(/idle for/i), "48");
  expect(refresh).toHaveBeenCalled();
});

test("a stored value with no matching preset still round-trips", async () => {
  // The server accepts 1..720; the presets are a convenience, not the schema. A value set by
  // hand (or by a future preset) must not silently reset the control to something else.
  renderPanel(block({ stale_hours: 72 }));
  expect(screen.getByLabelText(/idle for/i)).toHaveValue("72");
});

test("the copy says the session stays visible, because that is what makes this safe", () => {
  renderPanel();
  expect(
    screen.getByText(/stays on your Pulse cards and in the sidebar/i),
  ).toBeVisible();
});
