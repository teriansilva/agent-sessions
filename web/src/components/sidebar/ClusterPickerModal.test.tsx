import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import { CLUSTERS_CHANGED_EVENT } from "../../lib/clusterEvents";
import type { ClustersList } from "../../types/api";
import { ClusterPickerModal } from "./ClusterPickerModal";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, api: { clusters: vi.fn(), createCluster: vi.fn() } };
});

const A = "tc_0a1b2c3d4e5f";
const B = "tc_112233445566";
const row = (id: string, title: string, count: number) => ({
  id,
  title,
  kept: false,
  project_id: "p-a",
  count,
  ai_title: title,
  source: "ai" as const,
  summary: "",
});
const LIST: ClustersList = {
  clusters: [row(A, "Login flow", 3), row(B, "Billing", 2)],
  sweeps: {},
  unreadable: false,
  settings: { enabled: true, agent_tool: true, interval_minutes: 30, configured: true },
};

function picker(over: Partial<Parameters<typeof ClusterPickerModal>[0]> = {}) {
  const props = {
    heading: "Move to cluster",
    subjectLabel: "SESSION",
    subject: "Fix the login redirect",
    projectId: "p-a",
    onPick: vi.fn(),
    onCancel: vi.fn(),
    ...over,
  };
  render(<ClusterPickerModal {...props} />);
  return props;
}

beforeEach(() => {
  vi.mocked(api.clusters).mockReset().mockResolvedValue(LIST);
  vi.mocked(api.createCluster).mockReset();
});

test("lists the project's clusters, marks the current one and picks on a press", async () => {
  const p = picker({ currentId: A });
  expect(screen.getByRole("dialog", { name: "Move to cluster" })).toHaveAttribute("aria-modal", "true");
  expect(api.clusters).toHaveBeenCalledWith("p-a");
  expect(await screen.findByRole("button", { name: /Login flow/ })).toHaveAttribute(
    "aria-current",
    "true",
  );
  await userEvent.click(screen.getByRole("button", { name: /Billing/ }));
  expect(p.onPick).toHaveBeenCalledWith({ id: B, title: "Billing" });
});

test("focus starts on Cancel and moves to the first cluster once they load", async () => {
  picker();
  await waitFor(() => expect(screen.getByRole("button", { name: /Login flow/ })).toHaveFocus());
});

test("Escape and the close button cancel", async () => {
  const p = picker();
  await userEvent.keyboard("{Escape}");
  await userEvent.click(screen.getByRole("button", { name: "Close cluster dialog" }));
  expect(p.onCancel).toHaveBeenCalledTimes(2);
});

test("the excluded cluster is not offered (merging a cluster into itself)", async () => {
  picker({ excludeId: A });
  expect(await screen.findByRole("button", { name: /Billing/ })).toBeVisible();
  expect(screen.queryByRole("button", { name: /Login flow/ })).toBeNull();
});

test("Back to HQ's choice is offered only when asked for", async () => {
  const onRelease = vi.fn();
  picker({ onRelease });
  await userEvent.click(await screen.findByRole("button", { name: "Back to HQ’s choice" }));
  expect(onRelease).toHaveBeenCalled();
});

test("a new cluster is created in this project and picked", async () => {
  vi.mocked(api.createCluster).mockResolvedValue({ ...row("tc_aaaaaaaaaaaa", "Release prep", 0) });
  const p = picker({ allowCreate: true });
  const input = await screen.findByRole("textbox", { name: /New cluster/ });
  expect(input).toHaveAttribute("maxlength", "60");
  expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  await userEvent.type(input, " Release prep {Enter}");
  await waitFor(() => expect(api.createCluster).toHaveBeenCalledWith("p-a", "Release prep"));
  await waitFor(() =>
    expect(p.onPick).toHaveBeenCalledWith({ id: "tc_aaaaaaaaaaaa", title: "Release prep" }),
  );
});

test("a created cluster stays in the list with the draft cleared, so a retry never creates again", async () => {
  vi.mocked(api.createCluster).mockResolvedValue({ ...row("tc_aaaaaaaaaaaa", "Release prep", 0) });
  let changed = 0;
  const onChanged = () => {
    changed += 1;
  };
  window.addEventListener(CLUSTERS_CHANGED_EVENT, onChanged);
  // The caller's own write (the move) fails: the dialog stays open with its error.
  const onPick = vi.fn();
  const view = render(
    <ClusterPickerModal
      heading="Move to cluster"
      subjectLabel="SESSION"
      subject="Fix the login redirect"
      projectId="p-a"
      allowCreate
      onPick={onPick}
      onCancel={() => {}}
    />,
  );
  const input = await screen.findByRole("textbox", { name: /New cluster/ });
  await userEvent.type(input, "Release prep{Enter}");
  await waitFor(() => expect(onPick).toHaveBeenCalledTimes(1));
  expect(changed).toBe(1); // the new cluster is announced whether or not the move goes through
  window.removeEventListener(CLUSTERS_CHANGED_EVENT, onChanged);
  view.rerender(
    <ClusterPickerModal
      heading="Move to cluster"
      subjectLabel="SESSION"
      subject="Fix the login redirect"
      projectId="p-a"
      allowCreate
      onPick={onPick}
      onCancel={() => {}}
      error="the sessions could not be read right now"
    />,
  );
  expect(input).toHaveValue("");
  expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  expect(screen.getByTestId("cluster-picker-created")).toHaveTextContent(
    "Created “Release prep”. Pick it above to try the move again.",
  );
  // The retry is a pick of the cluster that already exists.
  await userEvent.click(screen.getByRole("button", { name: /Release prep/ }));
  expect(onPick).toHaveBeenLastCalledWith({ id: "tc_aaaaaaaaaaaa", title: "Release prep" });
  expect(onPick).toHaveBeenCalledTimes(2);
  expect(api.createCluster).toHaveBeenCalledTimes(1);
});

test("a refused create shows the server's reason and picks nothing", async () => {
  vi.mocked(api.createCluster).mockRejectedValue(
    new ApiError(409, "a project has at most 12 clusters"),
  );
  const p = picker({ allowCreate: true });
  await userEvent.type(await screen.findByRole("textbox", { name: /New cluster/ }), "x{Enter}");
  expect(await screen.findByRole("alert")).toHaveTextContent("a project has at most 12 clusters");
  expect(p.onPick).not.toHaveBeenCalled();
});

test("an unreadable store or a failed load is said, with Cancel still there", async () => {
  vi.mocked(api.clusters).mockResolvedValue({ ...LIST, clusters: [], unreadable: true });
  picker({ allowCreate: true });
  expect(await screen.findByRole("alert")).toHaveTextContent("Clusters are unavailable right now.");
  expect(screen.queryByRole("textbox", { name: /New cluster/ })).toBeNull();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeVisible();
});

test("the caller's error is shown and options lock while it is busy", async () => {
  picker({ busy: true, error: "that cluster belongs to another project" });
  expect(await screen.findByRole("alert")).toHaveTextContent("another project");
  expect(await screen.findByRole("button", { name: /Billing/ })).toBeDisabled();
});
