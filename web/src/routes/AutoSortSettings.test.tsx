import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig, AutoSortConfig } from "../types/api";
import { AutoSortSettings } from "./AutoSortSettings";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { setPrefs: vi.fn(), autoSortNow: vi.fn() } };
});

function block(over: Partial<AutoSortConfig> = {}): AutoSortConfig {
  return { enabled: false, interval_minutes: 30, configured: true, ...over };
}

function renderPanel(b: AutoSortConfig | undefined = block()) {
  const config = { csrf: "t", new_session_engines: [], terminal_backend: "ws", auto_sort: b };
  return render(
    <ConfigCtx.Provider value={config as AppConfig}>
      <AutoSortSettings />
    </ConfigCtx.Provider>,
  );
}

beforeEach(() => {
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({ auto_sort: block({ enabled: true }) });
  vi.mocked(api.autoSortNow).mockReset();
});

test("toggling enable persists the auto_sort flag (#424)", async () => {
  renderPanel(block({ enabled: false }));
  await userEvent.click(screen.getByRole("checkbox", { name: /enable auto-sort/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({ auto_sort: { enabled: true } });
});

test("the interval commits on blur within bounds; out-of-range reverts (#424)", async () => {
  renderPanel(block({ enabled: true, interval_minutes: 30 }));
  const input = screen.getByLabelText(/sort every/i);
  await userEvent.clear(input);
  await userEvent.type(input, "60");
  await userEvent.tab();
  expect(api.setPrefs).toHaveBeenCalledWith({ auto_sort: { interval_minutes: 60 } });

  vi.mocked(api.setPrefs).mockClear();
  await userEvent.clear(input);
  await userEvent.type(input, "2"); // below the 5-minute floor → revert, no save
  await userEvent.tab();
  expect(api.setPrefs).not.toHaveBeenCalled();
  expect(input).toHaveValue(30);
});

test("'Auto-sort now' is disabled until enabled AND the endpoint is configured (#424)", () => {
  renderPanel(block({ enabled: false, configured: true }));
  expect(screen.getByRole("button", { name: /auto-sort now/i })).toBeDisabled();
});

test("an unconfigured endpoint disables auto-sort and shows the hint (#424)", () => {
  renderPanel(block({ enabled: true, configured: false }));
  expect(screen.getByText(/configure the AI review endpoint above first/i)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /auto-sort now/i })).toBeDisabled();
});

test("running auto-sort reports how many sessions were assigned (#424)", async () => {
  vi.mocked(api.autoSortNow).mockResolvedValue({
    candidates: 5,
    scanned: 5,
    assigned: [
      { id: "claude:a", project_id: "p-1", confidence: 0.9 },
      { id: "claude:b", project_id: "p-1", confidence: 0.8 },
    ],
    low_confidence: 3,
    errors: 0,
  });
  renderPanel(block({ enabled: true, configured: true }));
  await userEvent.click(screen.getByRole("button", { name: /auto-sort now/i }));
  expect(api.autoSortNow).toHaveBeenCalledOnce();
  expect(await screen.findByText(/assigned 2 sessions to projects/i)).toBeInTheDocument();
});

test("running auto-sort with no confident matches says so (#424)", async () => {
  vi.mocked(api.autoSortNow).mockResolvedValue({
    candidates: 4,
    scanned: 4,
    assigned: [],
    low_confidence: 4,
    errors: 0,
  });
  renderPanel(block({ enabled: true, configured: true }));
  await userEvent.click(screen.getByRole("button", { name: /auto-sort now/i }));
  expect(
    await screen.findByText(/no confident matches among 4 unassigned sessions/i),
  ).toBeInTheDocument();
});
