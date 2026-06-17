import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig, AutoSortConfig } from "../types/api";
import { AutoSortSettings } from "./AutoSortSettings";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { setPrefs: vi.fn(), autoSortNow: vi.fn(), projectEntities: vi.fn() } };
});

function block(over: Partial<AutoSortConfig> = {}): AutoSortConfig {
  return {
    enabled: false,
    interval_minutes: 30,
    confidence_min: 0.7,
    max_per_pass: 8,
    prompt: "SORT PROMPT",
    configured: true,
    default_prompt: "DEFAULT SORT PROMPT",
    ...over,
  };
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
  vi.mocked(api.projectEntities).mockReset().mockResolvedValue({ projects: [] });
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

test("the confidence threshold commits on blur within bounds; out-of-range reverts (#459)", async () => {
  renderPanel(block({ enabled: true, confidence_min: 0.7 }));
  const input = screen.getByLabelText(/confidence threshold/i);
  await userEvent.clear(input);
  await userEvent.type(input, "0.55");
  await userEvent.tab();
  expect(api.setPrefs).toHaveBeenCalledWith({ auto_sort: { confidence_min: 0.55 } });

  vi.mocked(api.setPrefs).mockClear();
  await userEvent.clear(input);
  await userEvent.type(input, "0.3"); // below the 0.5 floor → revert, no save
  await userEvent.tab();
  expect(api.setPrefs).not.toHaveBeenCalled();
});

test("sessions-per-run commits on blur within bounds; out-of-range reverts (#459)", async () => {
  renderPanel(block({ enabled: true, max_per_pass: 8 }));
  const input = screen.getByLabelText(/sessions per run/i);
  await userEvent.clear(input);
  await userEvent.type(input, "20");
  await userEvent.tab();
  expect(api.setPrefs).toHaveBeenCalledWith({ auto_sort: { max_per_pass: 20 } });

  vi.mocked(api.setPrefs).mockClear();
  await userEvent.clear(input);
  await userEvent.type(input, "99"); // above the 50 ceiling → revert, no save
  await userEvent.tab();
  expect(api.setPrefs).not.toHaveBeenCalled();
});

test("the auto-sort prompt saves and resets to default (#459)", async () => {
  renderPanel(block({ enabled: true, prompt: "SORT PROMPT", default_prompt: "DEFAULT SORT PROMPT" }));
  const ta = screen.getByLabelText(/auto-sort prompt/i);
  await userEvent.type(ta, " extra");
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({ auto_sort: { prompt: "SORT PROMPT extra" } });

  vi.mocked(api.setPrefs).mockClear();
  await userEvent.click(screen.getByRole("button", { name: /reset to default/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({ auto_sort: { prompt: "DEFAULT SORT PROMPT" } });
});

test("'Auto-sort now' is disabled until enabled AND the endpoint is configured (#424)", () => {
  renderPanel(block({ enabled: false, configured: true }));
  expect(screen.getByRole("button", { name: /auto-sort now/i })).toBeDisabled();
});

test("an unconfigured endpoint disables auto-sort and shows the hint (#424)", () => {
  renderPanel(block({ enabled: true, configured: false }));
  expect(screen.getByText(/configure the AI endpoint above first/i)).toBeInTheDocument();
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
    near_misses: [],
  });
  renderPanel(block({ enabled: true, configured: true }));
  await userEvent.click(screen.getByRole("button", { name: /auto-sort now/i }));
  expect(api.autoSortNow).toHaveBeenCalledOnce();
  expect(await screen.findByText(/assigned 2 sessions to projects/i)).toBeInTheDocument();
});

test("a run with no matches lists the near-misses with project names (#459)", async () => {
  vi.mocked(api.projectEntities).mockResolvedValue({
    projects: [
      {
        id: "p-1",
        name: "superstatus",
        color: "",
        folders: [],
        default_folder: "",
        archived: false,
        created_at: 0,
        session_count: 0,
      },
    ],
  });
  vi.mocked(api.autoSortNow).mockResolvedValue({
    candidates: 3,
    scanned: 3,
    assigned: [],
    low_confidence: 3,
    errors: 0,
    near_misses: [{ id: "claude:a", project_id: "p-1", confidence: 0.62 }],
  });
  renderPanel(block({ enabled: true, configured: true }));
  await userEvent.click(screen.getByRole("button", { name: /auto-sort now/i }));
  expect(
    await screen.findByText(/no confident matches among 3 unassigned sessions/i),
  ).toBeInTheDocument();
  const closest = await screen.findByText(/lower the threshold to assign them/i);
  expect(closest.textContent).toMatch(/superstatus 0\.62/);
});
