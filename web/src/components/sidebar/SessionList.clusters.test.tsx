/** Session clusters in the sidebar (#1997): the row chip, the tree and head mounting, the filter
 *  the tree sets, "Move to cluster…", and what happens to a cluster filter when clusters go off. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../../app/config";
import { SectionStateContext } from "../../app/sectionState";
import { api, ApiError } from "../../lib/api";
import type { AppConfig, ClustersList, Session, SessionsPage } from "../../types/api";
import { FILTERS_OPEN_KEY } from "./Filters";
import { SessionList } from "./SessionList";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      sessions: vi.fn(),
      clusters: vi.fn(),
      placeSession: vi.fn(),
      createCluster: vi.fn(),
      patchCluster: vi.fn(),
    },
  };
});

const TC = "tc_0a1b2c3d4e5f";
const OTHER = "tc_112233445566";
const PROJECT = { kind: "project" as const, id: "p-a", name: "alpha", color: "" };
const SETTINGS = { enabled: true, agent_tool: true, interval_minutes: 30, configured: true };

function sess(n: string, title: string, over: Partial<Session> = {}): Session {
  return {
    id: `claude:${n}`,
    engine: "claude",
    uuid: n,
    short_uuid: n,
    cwd: "/home/m/alpha",
    project: PROJECT,
    last_mtime: Math.floor(Date.now() / 1000),
    first_user_message: "",
    title,
    sticky: false,
    archived: false,
    mission: null,
    cluster: null,
    ...over,
  };
}

const IN = sess("a", "Fix the login redirect", {
  cluster: { id: TC, title: "Login flow", kept: false },
});
const LOOSE = sess("b", "Bump vitest");
const HELD = sess("c", "Changelog", {
  mission: { id: "msn_1", title: "Release", state: "running" },
});

function page(rows: Session[], over: Partial<SessionsPage> = {}): SessionsPage {
  return {
    sessions: rows,
    next_offset: null,
    total: rows.length,
    facets: {
      projects: [{ ...PROJECT, count: 3 }],
      engines: ["claude"],
      missions: [{ id: "msn_1", title: "Release", state: "running", count: 1, by_project: { "p-a": 1 } }],
      no_mission: 2,
      clusters: [
        { id: TC, title: "Login flow", kept: false, project_id: "p-a", count: 1 },
        { id: OTHER, title: "Billing", kept: false, project_id: "p-a", count: 0 },
      ],
    },
    ...over,
  };
}

const LIST: ClustersList = {
  clusters: [
    { id: TC, title: "Login flow", kept: false, project_id: "p-a", count: 1, ai_title: "Login flow", source: "ai", summary: "Sign-in work." },
    { id: OTHER, title: "Billing", kept: false, project_id: "p-a", count: 0, ai_title: "Billing", source: "ai", summary: "" },
  ],
  sweeps: {},
  unreadable: false,
  settings: SETTINGS,
};

function list(
  clusters: AppConfig["clusters"] | null = SETTINGS,
  memory: Map<string, unknown> | null = null,
) {
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    ...(clusters ? { clusters } : {}),
  } as AppConfig;
  return render(
    <MemoryRouter>
      <SectionStateContext.Provider value={memory}>
        <ConfigCtx.Provider value={config}>
          <SessionList />
        </ConfigCtx.Provider>
      </SectionStateContext.Provider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(api.sessions).mockReset().mockResolvedValue(page([IN, LOOSE, HELD]));
  vi.mocked(api.clusters).mockReset().mockResolvedValue(LIST);
  vi.mocked(api.placeSession).mockReset();
});

const lastQuery = () => vi.mocked(api.sessions).mock.calls.at(-1)![0]!;

test("a clustered row shows its cluster; a mission-held or loose row does not", async () => {
  list();
  const row = (await screen.findByText("Fix the login redirect")).closest("li")!;
  expect(within(row).getByTestId("row-cluster-tag")).toHaveTextContent("Login flow");
  expect(
    within(screen.getByText("Bump vitest").closest("li")!).queryByTestId("row-cluster-tag"),
  ).toBeNull();
  expect(
    within(screen.getByText("Changelog").closest("li")!).queryByTestId("row-cluster-tag"),
  ).toBeNull();
});

test("selecting a cluster in the tree filters the list by it and shows its head", async () => {
  list();
  await screen.findByText("Fix the login redirect");
  await userEvent.click(screen.getByRole("button", { name: "Unfold alpha" }));
  await userEvent.click(screen.getAllByTestId("cluster-node")[0]);
  await waitFor(() => expect(lastQuery()).toMatchObject({ project: "p-a", cluster: TC }));
  const head = await screen.findByTestId("cluster-head");
  expect(within(head).getByTestId("cluster-head-title")).toHaveTextContent("Login flow");
  expect(await within(head).findByText("Sign-in work.")).toBeVisible();
});

test("picking another project in the tree drops the cluster filter", async () => {
  vi.mocked(api.sessions).mockResolvedValue(
    page([IN], {
      facets: {
        ...page([]).facets,
        projects: [
          { ...PROJECT, count: 3 },
          { kind: "project", id: "p-b", name: "beta", count: 1 },
        ],
      },
    }),
  );
  list();
  await screen.findByText("Fix the login redirect");
  // The tree is the project filter (#2187): there is no dropdown beside it.
  expect(screen.queryByRole("combobox", { name: "Filter by project" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Unfold alpha" }));
  await userEvent.click(screen.getAllByTestId("cluster-node")[0]);
  await waitFor(() => expect(lastQuery()).toMatchObject({ cluster: TC }));
  await userEvent.click(screen.getByRole("button", { name: /^beta/ }));
  await waitFor(() => expect(lastQuery().project).toBe("p-b"));
  expect(lastQuery().cluster).toBeUndefined();
});

test("cluster, then a mission from the dropdown, then Clear: every step names a true scope (#2187)", async () => {
  list();
  await screen.findByText("Fix the login redirect");
  await userEvent.click(screen.getByRole("button", { name: "Unfold alpha" }));
  await userEvent.click(screen.getAllByTestId("cluster-node")[0]);
  await waitFor(() => expect(lastQuery()).toMatchObject({ project: "p-a", cluster: TC }));
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("alpha › Login flow");
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Filter by mission" }), "msn_1");
  await waitFor(() => expect(lastQuery()).toMatchObject({ project: "p-a", mission: "msn_1" }));
  expect(lastQuery().cluster).toBeUndefined();
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("alpha › Release");
  await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  await waitFor(() => expect(lastQuery().mission).toBeUndefined());
  expect(lastQuery().project).toBeUndefined();
  expect(lastQuery().cluster).toBeUndefined();
  expect(screen.queryByTestId("filters-scope")).toBeNull();
  expect(screen.getByRole("button", { name: /^All sessions/ })).toHaveAttribute("aria-current", "true");
});

test("a folded block, opened again on this device, still names the scope in force (#2187)", async () => {
  localStorage.setItem(FILTERS_OPEN_KEY, "0");
  const memory = new Map<string, unknown>([
    ["sessions.filters", { q: "", project: "p-a", engine: "", mission: "", cluster: TC, archived: false }],
  ]);
  list(SETTINGS, memory);
  await screen.findByText("Fix the login redirect");
  const toggle = screen.getByTestId("filters-toggle");
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(lastQuery()).toMatchObject({ project: "p-a", cluster: TC });
  // The full scope is the chip's TEXT (the ellipsis is CSS), so it is in the toggle's name.
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("alpha › Login flow");
  expect(toggle).toHaveAccessibleName(/alpha › Login flow/);
});

test("folded and reloaded with a project the facets do not list, the header still names it and Clear removes it (Hermes, PR #2188)", async () => {
  localStorage.setItem(FILTERS_OPEN_KEY, "0");
  const memory = new Map<string, unknown>([
    ["sessions.filters", { q: "", project: "/home/m/legacy", engine: "", mission: "", cluster: "", archived: false }],
  ]);
  list(SETTINGS, memory);
  await screen.findByText("Fix the login redirect");
  expect(lastQuery()).toMatchObject({ project: "/home/m/legacy" });
  const toggle = screen.getByTestId("filters-toggle");
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("legacy");
  expect(toggle).toHaveAccessibleName(/legacy/);
  await userEvent.click(toggle);
  await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  await waitFor(() => expect(lastQuery().project).toBeUndefined());
  expect(screen.queryByTestId("filters-scope")).toBeNull();
});

test("the project tree sits inside the Filters fold, with no fold of its own (#2187)", async () => {
  list();
  await screen.findByText("Fix the login redirect");
  const toggle = screen.getByTestId("filters-toggle");
  const body = document.getElementById(toggle.getAttribute("aria-controls")!)!;
  expect(body).toContainElement(screen.getByTestId("project-tree"));
  expect(screen.queryByTestId("cluster-tree-toggle")).toBeNull();
  await userEvent.click(toggle);
  // Folding Filters folds the tree with it: there is one fold.
  expect(body).toHaveAttribute("hidden");
  expect(screen.getByTestId("project-tree")).not.toBeVisible();
});

test("with clusters off the tree has no clusters, there is no menu item, and a cluster filter from this visit is cleared", async () => {
  // The visit's view state still holds a cluster filter from before clusters were turned off.
  const memory = new Map<string, unknown>([
    [
      "sessions.filters",
      { q: "", project: "p-a", engine: "", mission: "", cluster: TC, archived: false },
    ],
  ]);
  list({ ...SETTINGS, enabled: false }, memory);
  await screen.findByText("Fix the login redirect");
  expect(vi.mocked(api.sessions).mock.calls[0][0]).toMatchObject({ cluster: TC });
  // The tree stays: it is the project filter (#2187). Only the clusters in it go.
  expect(screen.getByTestId("project-tree")).toBeInTheDocument();
  expect(screen.queryByTestId("cluster-node")).toBeNull();
  expect(screen.queryByTestId("cluster-none-node")).toBeNull();
  expect(screen.queryByTestId("cluster-head")).toBeNull();
  await waitFor(() => expect(lastQuery().cluster).toBeUndefined());
  expect(lastQuery().project).toBe("p-a");
  await userEvent.click(screen.getAllByRole("button", { name: "Session actions" })[0]);
  expect(screen.queryByRole("menuitem", { name: "Move session to a cluster" })).toBeNull();
});

test("an older server (no clusters block) shows nothing of clusters", async () => {
  list(null);
  await screen.findByText("Fix the login redirect");
  // The project tree is still the project filter (#2187); nothing in it is about clusters.
  expect(screen.getByTestId("project-tree")).toBeInTheDocument();
  expect(screen.queryByTestId("cluster-node")).toBeNull();
  expect(screen.queryByTestId("cluster-none-node")).toBeNull();
  expect(screen.queryByTestId("cluster-tree-unconfigured")).toBeNull();
});

test("Move to cluster places the session and the list re-reads", async () => {
  vi.mocked(api.placeSession).mockResolvedValue({
    id: LOOSE.id,
    cluster: { id: OTHER, title: "Billing", kept: false },
  });
  list();
  const row = (await screen.findByText("Bump vitest")).closest("li")!;
  await userEvent.click(within(row).getByRole("button", { name: "Session actions" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Move session to a cluster" }));
  const dialog = await screen.findByRole("dialog", { name: "Move to cluster" });
  expect(api.clusters).toHaveBeenCalledWith("p-a");
  // A session in no cluster has nothing to be handed back from.
  expect(within(dialog).queryByRole("button", { name: /Back to HQ/ })).toBeNull();
  const reads = vi.mocked(api.sessions).mock.calls.length;
  await userEvent.click(await within(dialog).findByRole("button", { name: /Billing/ }));
  await waitFor(() => expect(api.placeSession).toHaveBeenCalledWith(LOOSE.id, OTHER));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Move to cluster" })).toBeNull());
  await waitFor(() => expect(vi.mocked(api.sessions).mock.calls.length).toBeGreaterThan(reads));
});

test("a clustered session can be handed back to HQ's choice", async () => {
  vi.mocked(api.placeSession).mockResolvedValue({ id: IN.id, cluster: null });
  list();
  const row = (await screen.findByText("Fix the login redirect")).closest("li")!;
  await userEvent.click(within(row).getByRole("button", { name: "Session actions" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Move session to a cluster" }));
  await userEvent.click(await screen.findByRole("button", { name: "Back to HQ’s choice" }));
  await waitFor(() => expect(api.placeSession).toHaveBeenCalledWith(IN.id, null));
});

test("choosing the cluster a session is already in still places it there", async () => {
  // HQ put it there; the operator choosing it is what makes it stay (`placed`).
  vi.mocked(api.placeSession).mockResolvedValue({ id: IN.id, cluster: IN.cluster! });
  list();
  const row = (await screen.findByText("Fix the login redirect")).closest("li")!;
  await userEvent.click(within(row).getByRole("button", { name: "Session actions" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Move session to a cluster" }));
  const dialog = await screen.findByRole("dialog", { name: "Move to cluster" });
  const current = await within(dialog).findByRole("button", { name: /Login flow/ });
  expect(current).toHaveAttribute("aria-current", "true");
  await userEvent.click(current);
  await waitFor(() => expect(api.placeSession).toHaveBeenCalledWith(IN.id, TC));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Move to cluster" })).toBeNull());
});

test("a move that fails after New cluster is retried into that cluster, never by creating another", async () => {
  const made = { ...LIST.clusters[0], id: "tc_aaaaaaaaaaaa", title: "Release prep", count: 0 };
  vi.mocked(api.createCluster).mockResolvedValue(made);
  vi.mocked(api.placeSession)
    .mockRejectedValueOnce(new ApiError(503, "the sessions could not be read right now"))
    .mockResolvedValueOnce({
      id: LOOSE.id,
      cluster: { id: made.id, title: made.title, kept: true },
    });
  list();
  const row = (await screen.findByText("Bump vitest")).closest("li")!;
  await userEvent.click(within(row).getByRole("button", { name: "Session actions" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Move session to a cluster" }));
  const dialog = await screen.findByRole("dialog", { name: "Move to cluster" });
  await userEvent.type(
    await within(dialog).findByRole("textbox", { name: /New cluster/ }),
    "Release prep{Enter}",
  );
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(
    "the sessions could not be read right now",
  );
  expect(api.placeSession).toHaveBeenCalledTimes(1);
  // The created cluster is offered; the draft is gone, so Create cannot repeat it.
  expect(within(dialog).getByRole("textbox", { name: /New cluster/ })).toHaveValue("");
  await userEvent.click(within(dialog).getByRole("button", { name: /Release prep/ }));
  await waitFor(() => expect(api.placeSession).toHaveBeenCalledTimes(2));
  expect(api.placeSession).toHaveBeenLastCalledWith(LOOSE.id, made.id);
  expect(api.createCluster).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Move to cluster" })).toBeNull());
});

test("a refused move keeps the dialog open with the server's reason", async () => {
  vi.mocked(api.placeSession).mockRejectedValue(
    new ApiError(409, "a mission holds this session; the mission is its group"),
  );
  list();
  const row = (await screen.findByText("Bump vitest")).closest("li")!;
  await userEvent.click(within(row).getByRole("button", { name: "Session actions" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Move session to a cluster" }));
  await userEvent.click(await screen.findByRole("button", { name: /Billing/ }));
  const dialog = screen.getByRole("dialog", { name: "Move to cluster" });
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("a mission holds this session");
});

test("a mission-held session and one whose membership is unknown are not offered the move", async () => {
  const unknown = sess("d", "Unknown membership");
  delete (unknown as Partial<Session>).mission;
  delete (unknown as Partial<Session>).cluster;
  vi.mocked(api.sessions).mockResolvedValue(page([HELD, unknown]));
  list();
  for (const title of ["Changelog", "Unknown membership"]) {
    const row = (await screen.findByText(title)).closest("li")!;
    await userEvent.click(within(row).getByRole("button", { name: "Session actions" }));
    expect(screen.queryByRole("menuitem", { name: "Move session to a cluster" })).toBeNull();
    await userEvent.keyboard("{Escape}");
  }
});

test("a cluster filter the server could not apply is said in the tree", async () => {
  vi.mocked(api.sessions).mockResolvedValue(
    page([], { cluster_filter_unavailable: true, facets: { projects: [{ ...PROJECT, count: 3 }], engines: [] } }),
  );
  list();
  expect(await screen.findByText(/The cluster filter could not be applied/)).toBeVisible();
  expect(screen.getByText(/Clusters are unavailable right now/)).toBeVisible();
});
