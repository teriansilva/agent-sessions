import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import { CLUSTERS_CHANGED_EVENT } from "../../lib/clusterEvents";
import type { ClustersList, SessionClusterFacet } from "../../types/api";
import { ClusterHead } from "./ClusterHead";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      clusters: vi.fn(),
      patchCluster: vi.fn(),
      mergeCluster: vi.fn(),
      dissolveCluster: vi.fn(),
      recluster: vi.fn(),
    },
  };
});

const TC = "tc_0a1b2c3d4e5f";
const OTHER = "tc_112233445566";
const FACET: SessionClusterFacet = {
  id: TC,
  title: "Login flow",
  kept: false,
  project_id: "p-a",
  count: 3,
};
const SETTINGS = { enabled: true, agent_tool: true, interval_minutes: 30, configured: true };
const LIST: ClustersList = {
  clusters: [
    { ...FACET, ai_title: "Login flow", source: "ai", summary: "Three sessions on sign-in." },
    { id: OTHER, title: "Billing", kept: false, project_id: "p-a", count: 2, ai_title: "Billing", source: "ai", summary: "" },
  ],
  sweeps: {},
  unreadable: false,
  settings: SETTINGS,
};

let changed = 0;
const onChanged = () => {
  changed += 1;
};

function head(cluster: SessionClusterFacet | null = FACET, configured = true) {
  const onSelect = vi.fn();
  render(
    <ClusterHead
      cluster={cluster ?? undefined}
      clusterId={TC}
      projectName="alpha"
      configured={configured}
      onSelect={onSelect}
    />,
  );
  return onSelect;
}

beforeEach(() => {
  changed = 0;
  window.removeEventListener(CLUSTERS_CHANGED_EVENT, onChanged);
  window.addEventListener(CLUSTERS_CHANGED_EVENT, onChanged);
  vi.mocked(api.clusters).mockReset().mockResolvedValue(LIST);
  vi.mocked(api.patchCluster).mockReset().mockResolvedValue({ id: TC, title: "x", kept: true });
  vi.mocked(api.mergeCluster).mockReset().mockResolvedValue({ id: OTHER, title: "Billing", kept: false });
  vi.mocked(api.dissolveCluster).mockReset().mockResolvedValue({ ok: true });
  vi.mocked(api.recluster).mockReset();
});

test("shows the title, that HQ named it, and the summary read from the cluster list", async () => {
  head();
  expect(screen.getByTestId("cluster-head-title")).toHaveTextContent("Login flow");
  expect(screen.getByTestId("cluster-head-tag")).toHaveTextContent("AI title");
  expect(await screen.findByText("Three sessions on sign-in.")).toBeVisible();
  expect(api.clusters).toHaveBeenCalledWith("p-a");
});

test("rename saves the typed title and announces the change", async () => {
  head();
  await userEvent.click(screen.getByRole("button", { name: "Rename" }));
  const input = screen.getByRole("textbox", { name: "Cluster title" });
  expect(input).toHaveValue("Login flow");
  expect(input).toHaveAttribute("maxlength", "60");
  await userEvent.clear(input);
  await userEvent.type(input, "  Auth  {Enter}");
  await waitFor(() => expect(api.patchCluster).toHaveBeenCalledWith(TC, { title: "Auth" }));
  await waitFor(() => expect(changed).toBe(1));
  expect(screen.queryByRole("textbox", { name: "Cluster title" })).toBeNull();
});

test("an unchanged or empty rename writes nothing, and Escape leaves the editor", async () => {
  head();
  await userEvent.click(screen.getByRole("button", { name: "Rename" }));
  await userEvent.keyboard("{Enter}");
  expect(api.patchCluster).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Rename" }));
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("textbox", { name: "Cluster title" })).toBeNull();
});

test("Keep title keeps it; a kept cluster offers Let HQ rename and says what kept means", async () => {
  head();
  await userEvent.click(screen.getByRole("button", { name: "Keep title" }));
  await waitFor(() => expect(api.patchCluster).toHaveBeenCalledWith(TC, { keep: true }));
});

test("a kept cluster hands the title back", async () => {
  head({ ...FACET, kept: true });
  expect(screen.getByTestId("cluster-head-tag")).toHaveTextContent("Kept · your title");
  expect(screen.getByText(/It no longer changes the title/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Let HQ rename" }));
  await waitFor(() => expect(api.patchCluster).toHaveBeenCalledWith(TC, { keep: false }));
});

test("a refused write shows the server's reason and announces nothing", async () => {
  vi.mocked(api.patchCluster).mockRejectedValue(new ApiError(404, "that cluster isn't here"));
  head();
  await userEvent.click(screen.getByRole("button", { name: "Keep title" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("that cluster isn't here");
  expect(changed).toBe(0);
});

test("dissolve removes the cluster and points the list back at the project", async () => {
  const onSelect = head();
  await userEvent.click(screen.getByTestId("cluster-head-menu"));
  await userEvent.click(screen.getByRole("menuitem", { name: "Dissolve this cluster" }));
  await waitFor(() => expect(api.dissolveCluster).toHaveBeenCalledWith(TC));
  await waitFor(() => expect(onSelect).toHaveBeenCalledWith(""));
  expect(changed).toBe(1);
});

test("merge offers the project's other clusters and selects the target", async () => {
  const onSelect = head();
  await userEvent.click(screen.getByTestId("cluster-head-menu"));
  await userEvent.click(screen.getByRole("menuitem", { name: "Merge this cluster into another" }));
  const dialog = await screen.findByRole("dialog", { name: "Merge into" });
  expect(dialog).not.toHaveTextContent("Back to HQ");
  await userEvent.click(await screen.findByRole("button", { name: /Billing/ }));
  await waitFor(() => expect(api.mergeCluster).toHaveBeenCalledWith(TC, OTHER));
  await waitFor(() => expect(onSelect).toHaveBeenCalledWith(OTHER));
});

test("re-cluster runs this project's sweep and says how it went", async () => {
  vi.mocked(api.recluster).mockResolvedValue({
    projects: [{ project_id: "p-a", status: "error", error: "endpoint returned HTTP 502" }],
    deferred: 0,
  });
  head();
  await userEvent.click(screen.getByTestId("cluster-head-menu"));
  await userEvent.click(screen.getByRole("menuitem", { name: "Re-cluster alpha now" }));
  await waitFor(() => expect(api.recluster).toHaveBeenCalledWith("p-a"));
  expect(await screen.findByText(/HQ could not re-cluster: endpoint returned HTTP 502/)).toBeVisible();
});

test("re-cluster is disabled without an AI endpoint", async () => {
  head(FACET, false);
  await userEvent.click(screen.getByTestId("cluster-head-menu"));
  expect(screen.getByRole("menuitem", { name: "Re-cluster alpha now" })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
});

test("a cluster that is gone says so and offers the way back", async () => {
  const onSelect = head(null);
  expect(screen.getByText(/This cluster is no longer there/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Show the project" }));
  expect(onSelect).toHaveBeenCalledWith("");
});
