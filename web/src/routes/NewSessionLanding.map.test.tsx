/** #1648: the New session form hosted by the map's modal (`NewSessionModal`, the map host). The
 *  form emits what it launched; the host opens it — a window in place when the map can host one
 *  and has room, full screen otherwise — and nothing is created twice. */
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { WorkspaceCommandsCtx, type WorkspaceCommands } from "../app/workspaceWindows";
import { api } from "../lib/api";
import { projectPickerStore } from "../lib/projectPickerStore";
import { structuredModelsStore } from "../lib/structuredModelsStore";
import type { AppConfig } from "../types/api";
import { NewSessionModal } from "../components/overview/NewSessionModal";

const navigateMock = vi.fn();
vi.mock("react-router-dom", async (orig) => {
  const actual = await orig<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});
vi.mock("../lib/api", async (orig) => {
  const actual = await orig<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      projectPickerEntities: vi.fn(),
      setSessionProject: vi.fn(),
      chatNew: vi.fn(),
      structuredCreate: vi.fn(),
      structuredModels: vi.fn(),
      structuredSnapshot: vi.fn(),
      structuredStart: vi.fn(),
    },
  };
});

function commands(over: Partial<WorkspaceCommands> = {}): WorkspaceCommands {
  return {
    mapReady: true,
    hostable: true,
    hasRoom: true,
    room: 3,
    openKeys: new Set(),
    requestOpen: vi.fn(),
    openInMap: vi.fn(),
    newSession: { trigger: null },
    openNewSession: vi.fn(),
    closeNewSession: vi.fn(),
    ...over,
  };
}

function renderInModal(ws: WorkspaceCommands, { engines = ["claude"], onDone = vi.fn() } = {}) {
  const config: AppConfig = {
    csrf: "x",
    new_session_engines: engines,
    terminal_backend: "ws",
    default_project: "/d",
  };
  render(
    <ConfigCtx.Provider value={config}>
      <WorkspaceCommandsCtx.Provider value={ws}>
        <MemoryRouter initialEntries={["/overview"]}>
          <Routes>
            <Route path="/overview" element={<NewSessionModal onClose={onDone} />} />
          </Routes>
        </MemoryRouter>
      </WorkspaceCommandsCtx.Provider>
    </ConfigCtx.Provider>,
  );
  return { onDone };
}

async function noProject(user: ReturnType<typeof userEvent.setup>) {
  const group = await screen.findByRole("radiogroup", { name: "Project" });
  const radios = (await within(group).findAllByRole("radio")) as HTMLInputElement[];
  await user.click(radios.find((r) => r.value === "")!);
}

beforeEach(() => {
  projectPickerStore.reset();
  structuredModelsStore.reset();
  navigateMock.mockReset();
  vi.mocked(api.projectPickerEntities).mockReset().mockResolvedValue({ projects: [] });
  vi.mocked(api.setSessionProject).mockReset().mockResolvedValue({ id: "x", project_id: "" });
});

test("on a map with room, a console start opens its window in place with its fresh choices", async () => {
  const user = userEvent.setup();
  const ws = commands();
  const { onDone } = renderInModal(ws);
  await noProject(user);
  expect(screen.queryByTestId("new-session-map-full")).toBeNull();
  await user.click(screen.getByRole("button", { name: "Start session" }));
  expect(ws.requestOpen).toHaveBeenCalledTimes(1);
  const [seed, fresh] = vi.mocked(ws.requestOpen).mock.calls[0];
  expect(seed).toMatchObject({ engine: "claude", title: "New session" });
  expect(seed.key).toBe(`claude:${seed.id}`);
  expect(fresh).toMatchObject({ cwd: "/d" });
  expect(onDone).toHaveBeenCalledTimes(1);
  expect(navigateMock).not.toHaveBeenCalled(); // the map stays: no navigation at all
});

test("a full map says so before Start, and Start opens full screen with the same choices", async () => {
  const user = userEvent.setup();
  const ws = commands({ hasRoom: false, room: 0 });
  const { onDone } = renderInModal(ws);
  await noProject(user);
  expect(screen.getByTestId("new-session-map-full")).toHaveTextContent("The map is full.");
  await user.click(screen.getByRole("button", { name: "Start full screen" }));
  expect(ws.requestOpen).not.toHaveBeenCalled();
  expect(onDone).not.toHaveBeenCalled();
  const [path, opts] = navigateMock.mock.calls[0] as [string, { state: { fresh: unknown } }];
  expect(path).toMatch(/^\/s\/claude\/[0-9a-f-]{36}$/);
  expect(opts.state.fresh).toMatchObject({ cwd: "/d" });
});

test("a map that cannot host a window (a phone) opens the session full screen", async () => {
  const user = userEvent.setup();
  const ws = commands({ mapReady: false, hostable: false });
  renderInModal(ws);
  await noProject(user);
  expect(screen.queryByTestId("new-session-map-full")).toBeNull();
  await user.click(screen.getByRole("button", { name: "Start session" }));
  expect(ws.requestOpen).not.toHaveBeenCalled();
  expect(navigateMock.mock.calls[0][0]).toMatch(/^\/s\/claude\//);
});

test("a server-created session opens in place once, and the create holds the modal while in flight", async () => {
  const user = userEvent.setup();
  let resolve!: (v: { id: string }) => void;
  vi.mocked(api.chatNew).mockReset().mockReturnValue(new Promise((r) => (resolve = r)));
  const ws = commands();
  const { onDone } = renderInModal(ws, { engines: ["apichat"] });
  await noProject(user);
  const startBtn = screen.getByRole("button", { name: "Start session" });
  await user.click(startBtn);
  await user.click(startBtn);
  expect(api.chatNew).toHaveBeenCalledTimes(1); // single-flight, as on the page
  expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
  // The modal is held open while the create is in flight.
  expect(screen.getByRole("button", { name: "Close new session" })).toBeDisabled();
  await user.keyboard("{Escape}");
  expect(onDone).not.toHaveBeenCalled();
  await act(async () => resolve({ id: "apichat:0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b" }));
  expect(ws.requestOpen).toHaveBeenCalledWith({
    key: "apichat:0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b",
    engine: "apichat",
    id: "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b",
    title: "New session",
  });
  expect(onDone).toHaveBeenCalledTimes(1);
  expect(navigateMock).not.toHaveBeenCalled();
});

test("a detour clicked while a create is in flight does nothing, and the session still opens once (Hermes on #1717)", async () => {
  const user = userEvent.setup();
  let resolve!: (v: { id: string }) => void;
  vi.mocked(api.chatNew).mockReset().mockReturnValue(new Promise((r) => (resolve = r)));
  const ws = commands();
  const { onDone } = renderInModal(ws, { engines: ["apichat"] });
  await noProject(user);
  await user.click(screen.getByRole("button", { name: "Start session" }));
  // New project leaves the map for the wizard: held while the create is in flight.
  const detour = screen.getByRole("link", { name: /new project/i });
  await user.click(detour);
  detour.focus();
  await user.keyboard("{Enter}");
  expect(navigateMock).not.toHaveBeenCalled();
  expect(screen.getByTestId("new-session-modal")).toBeInTheDocument();
  await act(async () => resolve({ id: "apichat:0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b" }));
  expect(ws.requestOpen).toHaveBeenCalledTimes(1);
  expect(onDone).toHaveBeenCalledTimes(1);
  expect(navigateMock).not.toHaveBeenCalled();
});

test("Ctrl+Enter starts from inside the modal, never while another dialog is up", async () => {
  const user = userEvent.setup();
  const ws = commands();
  renderInModal(ws);
  await noProject(user);
  const other = document.createElement("div");
  other.setAttribute("aria-modal", "true");
  document.body.appendChild(other); // a nested dialog opened from the form
  await user.keyboard("{Control>}{Enter}{/Control}");
  expect(ws.requestOpen).not.toHaveBeenCalled();
  other.remove();
  await user.keyboard("{Control>}{Enter}{/Control}");
  expect(ws.requestOpen).toHaveBeenCalledTimes(1);
});

test("in the modal the page's heading goes, and the detours say they leave the map", async () => {
  renderInModal(commands());
  await screen.findByRole("radiogroup", { name: "Project" });
  expect(screen.queryByRole("heading", { name: /start a new session/i })).toBeNull();
  expect(screen.getByRole("link", { name: /new project/i })).toHaveTextContent("leaves the map");
});
