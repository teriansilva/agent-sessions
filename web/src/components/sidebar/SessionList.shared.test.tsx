// The Shared tab of the session list (#2198): sessions peers share with this instance have their
// own scope beside Active and Archived, and no longer follow the local rows.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { refreshRemoteShares, resetRemoteSharesForTest } from "../../app/remoteShares";
import { SectionStateContext } from "../../app/sectionState";
import type { Filters } from "../../hooks/useSessionsList";
import { api } from "../../lib/api";
import type { RemoteShare } from "../../lib/federation";
import type { Session, SessionsPage } from "../../types/api";
import { SessionList } from "./SessionList";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, api: { sessions: vi.fn(), federationRemote: vi.fn() } };
});

const mockSessions = vi.mocked(api.sessions);
const mockRemote = vi.mocked(api.federationRemote);

const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const LAPTOP = "b7nd4qzse2vkx6mhp3ytw5caj4ruo2gf";

function share(n: number, title: string, peer_name = "gpu-box", over: Partial<RemoteShare> = {}): RemoteShare {
  return {
    key: `k${n}`,
    peer_id: peer_name === "laptop" ? LAPTOP : GPU,
    peer_name,
    connected: true,
    grant_id: `grant-${String(n).padStart(4, "0")}-abcdefgh`,
    engine: "e",
    native_id: `n${n}`,
    title,
    level: "view",
    expires_at: 1_800_000_000,
    revision: 1,
    seen_at: 1_790_000_000,
    ...over,
  };
}
const THREE = [
  share(1, "Migrate the billing tables"),
  share(2, "Bench the new tokenizer"),
  share(3, "Release notes 0.21", "laptop"),
];

function sess(id: string, title: string): Session {
  return {
    id,
    engine: id.split(":")[0],
    uuid: id.split(":")[1],
    short_uuid: id.slice(0, 8),
    cwd: "/home/m/x",
    project: { kind: "folder" as const, id: "/home/m/x", name: "/home/m/x" },
    last_mtime: Math.floor(Date.now() / 1000),
    first_user_message: "",
    title,
    sticky: false,
    archived: false,
  };
}
const page = (sessions: Session[], over: Partial<SessionsPage> = {}): SessionsPage => ({
  sessions,
  next_offset: null,
  total: sessions.length,
  facets: { projects: [], engines: [] },
  ...over,
});

const BASE: Filters = { q: "", project: "", engine: "", mission: "", cluster: "", archived: false };

/** The list, with the section memory a visit keeps: `saved` is the remembered filter model. */
function show(saved?: Partial<Filters>) {
  const memory = new Map<string, unknown>();
  if (saved) memory.set("sessions.filters", { ...BASE, ...saved });
  render(
    <SectionStateContext.Provider value={memory}>
      <MemoryRouter>
        <SessionList />
      </MemoryRouter>
    </SectionStateContext.Provider>,
  );
  return { saved: () => memory.get("sessions.filters") as Filters };
}

const tab = (name: RegExp | string) => screen.queryByRole("tab", { name });
const rows = () => screen.queryAllByTestId("remote-row");

beforeEach(() => {
  resetRemoteSharesForTest();
  mockSessions.mockReset();
  mockRemote.mockReset();
  mockSessions.mockResolvedValue(page([sess("claude:a", "Local one"), sess("claude:b", "Local two")]));
  mockRemote.mockResolvedValue({ remote: THREE });
});

test("no third tab while nothing is shared; with a share it carries the count", async () => {
  mockRemote.mockResolvedValue({ remote: [] });
  show();
  expect(await screen.findByText("Local one")).toBeInTheDocument();
  expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Active", "Archived"]);
  mockRemote.mockResolvedValue({ remote: THREE });
  await act(() => refreshRemoteShares());
  expect(tab(/^Shared/)).toHaveTextContent("Shared 3");
  expect(screen.getByTestId("shared-count")).toHaveTextContent("3");
});

test("the count is every shared session, two digits too, never the number the search matches", async () => {
  const twelve = Array.from({ length: 12 }, (_, i) => share(i + 1, `Shared session ${i + 1}`));
  mockRemote.mockResolvedValue({ remote: twelve });
  show({ shared: true, sharedQ: "session 12" });
  await waitFor(() => expect(rows()).toHaveLength(1));
  expect(screen.getByTestId("shared-count")).toHaveTextContent("12");
});

test("Active and Archived list only this instance's sessions", async () => {
  const user = userEvent.setup();
  show();
  expect(await screen.findByText("Local one")).toBeInTheDocument();
  await waitFor(() => expect(tab(/^Shared/)).toBeInTheDocument());
  expect(rows()).toHaveLength(0);
  expect(screen.queryByText("Migrate the billing tables")).toBeNull();
  await user.click(tab("Archived")!);
  await waitFor(() => expect(mockSessions.mock.lastCall?.[0]).toMatchObject({ archived: true }));
  expect(rows()).toHaveLength(0);
  // An empty local list does not bring them back either.
  mockSessions.mockResolvedValue(page([]));
  await user.click(tab("Active")!);
  expect(await screen.findByText("No sessions yet.")).toBeInTheDocument();
  expect(rows()).toHaveLength(0);
});

test("Shared lists the shared rows and nothing of the local list", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(page([sess("claude:a", "Local one")], { next_offset: 20, total: 40 }));
  show();
  expect(await screen.findByText("Local one")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
  await user.click((await screen.findByRole("tab", { name: /^Shared/ }))!);
  expect(tab(/^Shared/)).toHaveAttribute("aria-selected", "true");
  expect(rows().map((r) => within(r).getByRole("link").getAttribute("href"))).toEqual(
    THREE.map((r) => `/remote/${r.peer_id}/${r.grant_id}`),
  );
  expect(screen.getByRole("list", { name: "3 sessions shared by peers" })).toBeInTheDocument();
  // No local row, no paging, no row action, no local filter.
  expect(screen.queryByText("Local one")).toBeNull();
  expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Session actions" })).toBeNull();
  expect(screen.queryByLabelText("Search sessions")).toBeNull();
  expect(screen.queryByLabelText("Clear filters")).toBeNull();
});

test("search narrows by title or by peer name, whatever the case", async () => {
  const user = userEvent.setup();
  show({ shared: true });
  await waitFor(() => expect(rows()).toHaveLength(3));
  const search = screen.getByLabelText("Search shared sessions");
  await user.type(search, "BILLING");
  expect(rows().map((r) => r.textContent)).toEqual([expect.stringContaining("Migrate the billing tables")]);
  await user.clear(search);
  await user.type(search, "Laptop");
  expect(rows().map((r) => r.textContent)).toEqual([expect.stringContaining("Release notes 0.21")]);
  await user.clear(search);
  await user.type(search, "xyz");
  expect(rows()).toHaveLength(0);
  expect(screen.getByTestId("shared-empty")).toHaveTextContent(
    "No shared session matches.Clear the search to see all 3.",
  );
});

test("Clear search clears the Shared search only: the scope and the hidden local filters stay", async () => {
  const user = userEvent.setup();
  const local = { q: "deploy", project: "p-1", engine: "claude", mission: "msn_1", archived: true };
  const { saved } = show({ ...local, shared: true, sharedQ: "xyz" });
  expect(await screen.findByText("No shared session matches.")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Clear search" }));
  await waitFor(() => expect(rows()).toHaveLength(3));
  expect(tab(/^Shared/)).toHaveAttribute("aria-selected", "true");
  expect(screen.getByLabelText("Search shared sessions")).toHaveValue("");
  expect(saved()).toMatchObject({ ...local, shared: true, sharedQ: "" });
});

test("back on Archived and on Active the saved local filters apply again", async () => {
  const user = userEvent.setup();
  const local = { q: "deploy", project: "p-1", engine: "claude", mission: "msn_1" };
  mockSessions.mockResolvedValue(
    page([sess("claude:a", "Local one")], {
      facets: { projects: [], engines: ["claude", "other"], missions: [{ id: "msn_1", title: "Release", count: 1 }] },
    }),
  );
  show({ ...local, archived: true, shared: true, sharedQ: "billing" });
  await waitFor(() => expect(rows()).toHaveLength(1));
  // On Shared the local controls are not shown.
  expect(screen.queryByLabelText("Filter by agent")).toBeNull();
  expect(screen.queryByLabelText("Filter by mission")).toBeNull();
  const before = mockSessions.mock.calls.length;

  await user.click(tab("Archived")!);
  expect(await screen.findByText("Local one")).toBeInTheDocument();
  expect(screen.getByLabelText("Search sessions")).toHaveValue("deploy");
  expect(screen.getByLabelText("Filter by agent")).toHaveValue("claude");
  expect(screen.getByLabelText("Filter by mission")).toHaveValue("msn_1");
  // Leaving Shared for the scope the query already had asks the server nothing new.
  expect(mockSessions.mock.calls.length).toBe(before);
  expect(mockSessions.mock.lastCall?.[0]).toMatchObject({ ...local, archived: true });

  await user.click(tab("Active")!);
  await waitFor(() =>
    expect(mockSessions.mock.lastCall?.[0]).toMatchObject({ ...local, archived: false }),
  );
  expect(screen.getByLabelText("Search sessions")).toHaveValue("deploy");
  // The Shared search is its own value: it is there again on return, and never the local one.
  await user.click(tab(/^Shared/)!);
  expect(screen.getByLabelText("Search shared sessions")).toHaveValue("billing");
});

test("no /api/sessions request differs because of the Shared scope", async () => {
  const user = userEvent.setup();
  show({ project: "p-1", archived: true });
  expect(await screen.findByText("Local one")).toBeInTheDocument();
  const first = mockSessions.mock.calls.map((c) => c[0]);
  expect(first).toHaveLength(1);
  await user.click((await screen.findByRole("tab", { name: /^Shared/ }))!);
  await user.type(screen.getByLabelText("Search shared sessions"), "billing");
  // Past the local search's debounce: a change of query would have been sent by now.
  await new Promise((r) => setTimeout(r, 400));
  expect(mockSessions.mock.calls.map((c) => c[0])).toEqual(first);
  for (const [q] of mockSessions.mock.calls) {
    expect(Object.keys(q ?? {}).sort()).toEqual(
      ["archived", "cluster", "engine", "limit", "mission", "offset", "project", "q"].sort(),
    );
    expect(q).toMatchObject({ q: "", project: "p-1", archived: true });
  }
});

// ---- a remembered Shared scope: back to Active only on a successful, empty read ----

test("fallback 1: a delayed first read keeps Shared, and it stays when the read is not empty", async () => {
  let answer!: (v: { remote: RemoteShare[] }) => void;
  mockRemote.mockReturnValue(new Promise((r) => (answer = r)));
  show({ shared: true });
  await waitFor(() => expect(mockSessions).toHaveBeenCalled());
  // The store's array is empty and nothing was read: that is not "nothing is shared".
  expect(tab(/^Shared/)).toHaveAttribute("aria-selected", "true");
  expect(screen.getByTestId("shared-empty")).toHaveTextContent("Reading shared sessions…");
  expect(screen.queryByText("Local one")).toBeNull();
  await act(async () => answer({ remote: THREE }));
  await waitFor(() => expect(rows()).toHaveLength(3));
  expect(tab(/^Shared/)).toHaveAttribute("aria-selected", "true");
});

test("fallback 2: a successful read that is empty goes back to Active", async () => {
  let answer!: (v: { remote: RemoteShare[] }) => void;
  mockRemote.mockReturnValue(new Promise((r) => (answer = r)));
  // Active, whatever lay under Shared: here the Archived flag.
  const { saved } = show({ shared: true, project: "p-1", archived: true });
  await waitFor(() => expect(mockSessions).toHaveBeenCalled());
  expect(tab(/^Shared/)).toHaveAttribute("aria-selected", "true");
  await act(async () => answer({ remote: [] }));
  expect(await screen.findByText("Local one")).toBeInTheDocument();
  expect(tab(/^Shared/)).toBeNull();
  expect(tab("Active")).toHaveAttribute("aria-selected", "true");
  expect(saved()).toMatchObject({ shared: false, project: "p-1", archived: false });
});

test("fallback 3: a failed read keeps Shared and says the list could not be read", async () => {
  mockRemote.mockRejectedValue(new Error("down"));
  show({ shared: true });
  expect(await screen.findByText("The shared sessions could not be read.")).toBeInTheDocument();
  expect(tab(/^Shared/)).toHaveAttribute("aria-selected", "true");
  expect(screen.queryByTestId("shared-count")).toBeNull();
  expect(screen.queryByText("Local one")).toBeNull();
  // The next read succeeds: the rows are listed, on the tab the operator never left.
  mockRemote.mockResolvedValue({ remote: THREE });
  await act(() => refreshRemoteShares());
  expect(rows()).toHaveLength(3);
  expect(tab(/^Shared/)).toHaveAttribute("aria-selected", "true");
});

test("the selected tab stays when the last share ends, and goes when the operator leaves it", async () => {
  const user = userEvent.setup();
  show({ shared: true });
  await waitFor(() => expect(rows()).toHaveLength(3));
  mockRemote.mockResolvedValue({ remote: [] });
  await act(() => refreshRemoteShares());
  expect(tab(/^Shared/)).toHaveAttribute("aria-selected", "true");
  expect(tab(/^Shared/)).toHaveTextContent(/^Shared$/);
  expect(screen.getByTestId("shared-empty")).toHaveTextContent(
    "Nothing is shared with you right now.A paired instance shares a session from that session’s Share → Peer…",
  );
  expect(screen.queryByText("Local one")).toBeNull();
  await user.click(tab("Active")!);
  expect(await screen.findByText("Local one")).toBeInTheDocument();
  expect(tab(/^Shared/)).toBeNull();
  expect(tab("Active")).toHaveFocus();
});
