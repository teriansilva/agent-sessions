import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { AppConfig, ClustersConfig, ClustersList } from "../types/api";
import { ClustersSettings } from "./ClustersSettings";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { setPrefs: vi.fn(), clusters: vi.fn(), recluster: vi.fn() } };
});

const block = (over: Partial<ClustersConfig> = {}): ClustersConfig => ({
  enabled: true,
  agent_tool: true,
  interval_minutes: 30,
  configured: true,
  ...over,
});
const list = (over: Partial<ClustersList> = {}): ClustersList => ({
  clusters: [
    { id: "tc_0a1b2c3d4e5f", title: "Login flow", kept: false, project_id: "p-a", count: 3, ai_title: "Login flow", source: "ai", summary: "" },
  ],
  sweeps: { "p-a": { at: Math.floor(Date.now() / 1000) - 240, error: null } },
  unreadable: false,
  settings: block(),
  ...over,
});

function panel(b: ClustersConfig | undefined = block(), refresh: () => void = () => {}) {
  const config = { csrf: "t", new_session_engines: [], terminal_backend: "ws", clusters: b };
  return render(
    <MemoryRouter>
      <ConfigRefreshCtx.Provider value={refresh}>
        <ConfigCtx.Provider value={config as AppConfig}>
          <ClustersSettings />
        </ConfigCtx.Provider>
      </ConfigRefreshCtx.Provider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({ clusters: block({ enabled: false }) });
  vi.mocked(api.clusters).mockReset().mockResolvedValue(list());
  vi.mocked(api.recluster).mockReset();
});

test("on by default, with the last sweep and what it found", async () => {
  panel();
  expect(screen.getByRole("checkbox", { name: "Group sessions into clusters" })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Sessions see their cluster" })).toBeChecked();
  expect(screen.getByRole("combobox", { name: "Re-cluster" })).toHaveValue("30");
  await waitFor(() =>
    expect(screen.getByTestId("clusters-status")).toHaveTextContent(
      /^On · last sweep .* · 1 project · 1 cluster$/,
    ),
  );
});

test("each control saves its own field and refreshes the shared config", async () => {
  const refresh = vi.fn();
  panel(block(), refresh);
  await userEvent.click(screen.getByRole("checkbox", { name: "Group sessions into clusters" }));
  expect(api.setPrefs).toHaveBeenLastCalledWith({ clusters: { enabled: false } });
  await waitFor(() => expect(refresh).toHaveBeenCalled());
  // The saved block came back with clusters off: the dependent controls lock.
  await waitFor(() =>
    expect(screen.getByRole("checkbox", { name: "Sessions see their cluster" })).toBeDisabled(),
  );
});

test("the agent tool and the interval save too", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({ clusters: block({ agent_tool: false }) });
  panel();
  await userEvent.click(screen.getByRole("checkbox", { name: "Sessions see their cluster" }));
  expect(api.setPrefs).toHaveBeenLastCalledWith({ clusters: { agent_tool: false } });
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Re-cluster" }), "240");
  expect(api.setPrefs).toHaveBeenLastCalledWith({ clusters: { interval_minutes: 240 } });
});

test("without an AI endpoint it says so and cannot re-cluster", () => {
  panel(block({ configured: false }));
  expect(screen.getByTestId("clusters-status")).toHaveTextContent("Waiting for an AI endpoint");
  expect(screen.getByRole("link", { name: "Endpoint & model" })).toHaveAttribute(
    "href",
    "/settings/ai-endpoint",
  );
  expect(screen.getByRole("button", { name: "Re-cluster now" })).toBeDisabled();
});

test("re-cluster now reports what HQ did", async () => {
  vi.mocked(api.recluster).mockResolvedValue({
    projects: [
      { project_id: "p-a", status: "committed" },
      { project_id: "p-b", status: "unchanged" },
    ],
    deferred: 3,
  });
  panel();
  await userEvent.click(screen.getByRole("button", { name: "Re-cluster now" }));
  expect(
    await screen.findByText("HQ looked at 2 projects and regrouped 1. 3 more will follow on the schedule."),
  ).toBeVisible();
  // The status line is read again after the run.
  await waitFor(() => expect(api.clusters).toHaveBeenCalledTimes(2));
});

test("a failed sweep is named, from the run and from the stored record", async () => {
  vi.mocked(api.clusters).mockResolvedValue(
    list({ sweeps: { "p-a": { at: 1, error: "endpoint returned HTTP 502" } } }),
  );
  vi.mocked(api.recluster).mockRejectedValue(new ApiError(409, "session clusters are turned off"));
  panel();
  expect(await screen.findByTestId("clusters-sweep-error")).toHaveTextContent(
    "endpoint returned HTTP 502",
  );
  await userEvent.click(screen.getByRole("button", { name: "Re-cluster now" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("session clusters are turned off");
});

test("an unreadable store is said, never shown as no clusters", async () => {
  vi.mocked(api.clusters).mockResolvedValue(list({ clusters: [], sweeps: {}, unreadable: true }));
  panel();
  expect(await screen.findByTestId("clusters-sweep-error")).toHaveTextContent(
    "The cluster store cannot be read right now.",
  );
  expect(screen.getByTestId("clusters-status")).toHaveTextContent(/^On$/);
});

test("a failed save says so and keeps the value", async () => {
  vi.mocked(api.setPrefs).mockRejectedValue(new Error("offline"));
  panel();
  await userEvent.click(screen.getByRole("checkbox", { name: "Group sessions into clusters" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t save");
  expect(screen.getByRole("checkbox", { name: "Group sessions into clusters" })).toBeChecked();
});
