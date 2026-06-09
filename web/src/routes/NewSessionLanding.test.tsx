import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import { mintNewSessionId } from "../lib/newSession";
import type { AppConfig } from "../types/api";
import { NewSessionLanding } from "./NewSessionLanding";

const navigateMock = vi.fn();
vi.mock("react-router-dom", async (orig) => {
  const actual = await orig<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});
vi.mock("../lib/api", async (orig) => {
  const actual = await orig<typeof import("../lib/api")>();
  return {
    ...actual,
    api: { projects: vi.fn(), setPrefs: vi.fn().mockResolvedValue({}), mkdir: vi.fn() },
  };
});
const mockProjects = vi.mocked(api.projects);

function renderLanding(engines = ["claude"], extra: Partial<AppConfig> = {}) {
  const config: AppConfig = {
    csrf: "x",
    new_session_engines: engines,
    terminal_backend: "ws",
    ...extra,
  };
  return render(
    <ConfigCtx.Provider value={config}>
      <MemoryRouter>
        <NewSessionLanding />
      </MemoryRouter>
    </ConfigCtx.Provider>,
  );
}

beforeEach(() => {
  navigateMock.mockReset();
  mockProjects.mockReset();
});

test("starts a session: mints an id and navigates with the fresh launch params", async () => {
  const user = userEvent.setup();
  mockProjects.mockResolvedValue({ projects: [{ cwd: "/home/m/proj", label: "/home/m/proj" }] });
  renderLanding();
  await screen.findByRole("option", { name: "/home/m/proj" });

  await user.click(screen.getByRole("button", { name: /start session/i }));
  expect(navigateMock).toHaveBeenCalledTimes(1);
  const [path, opts] = navigateMock.mock.calls[0] as [string, { state: { fresh: unknown } }];
  expect(path).toMatch(/^\/s\/claude\/[0-9a-f-]{36}$/); // engine + a minted uuid
  expect(opts.state.fresh).toEqual({ cwd: "/home/m/proj", bypass: true });
});

test.each([
  ["claude", /^[0-9a-f-]{36}$/],
  ["codex", /^[0-9a-f-]{36}$/],
  ["gemini", /^[0-9a-f-]{36}$/],
  ["opencode", /^new-[0-9a-f-]{36}$/], // #163: opencode needs the new-<uuid> placeholder
])("mintNewSessionId(%s) → %s", (engine, shape) => {
  expect(mintNewSessionId(engine)).toMatch(shape);
});

test("opencode new session navigates to a new-<uuid> placeholder, not a bare uuid (#163)", async () => {
  const user = userEvent.setup();
  mockProjects.mockResolvedValue({ projects: [{ cwd: "/home/m/proj", label: "/home/m/proj" }] });
  renderLanding(["opencode"]); // single engine → opencode is the effective selection
  await screen.findByRole("option", { name: "/home/m/proj" });

  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [path] = navigateMock.mock.calls[0] as [string, unknown];
  // A bare uuid here would 4404 on the opencode new=1 launch.
  expect(path).toMatch(/^\/s\/opencode\/new-[0-9a-f-]{36}$/);
});

test("the agent picker is hidden when there is only one engine", async () => {
  mockProjects.mockResolvedValue({ projects: [{ cwd: "/x", label: "/x" }] });
  renderLanding(["claude"]);
  await screen.findByRole("option", { name: "/x" });
  expect(screen.queryByText("Agent")).not.toBeInTheDocument();
});

test("the agent picker is shown with more than one engine", async () => {
  mockProjects.mockResolvedValue({ projects: [{ cwd: "/x", label: "/x" }] });
  renderLanding(["claude", "opencode"]);
  expect(await screen.findByText("Agent")).toBeInTheDocument();
});

test("Start is disabled until a project is available", async () => {
  mockProjects.mockResolvedValue({ projects: [] });
  renderLanding();
  // No projects → the only option is the placeholder and Start stays disabled.
  await screen.findByRole("option", { name: /no projects found/i });
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
});

test("pre-selects the default project when it is pickable (#335 Phase 2)", async () => {
  mockProjects.mockResolvedValue({
    projects: [
      { cwd: "/a", label: "/a" },
      { cwd: "/b", label: "/b" },
    ],
  });
  renderLanding(["claude"], { default_project: "/b" });
  await screen.findByRole("option", { name: "/b" });
  // the select lands on the default, not the first option
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("/b");
  await userEvent.click(screen.getByRole("button", { name: /start session/i }));
  const [, opts] = navigateMock.mock.calls[0] as [string, { state: { fresh: { cwd: string } } }];
  expect(opts.state.fresh.cwd).toBe("/b");
});

test("falls back to the first project when the default is stale (#335 Phase 2)", async () => {
  mockProjects.mockResolvedValue({ projects: [{ cwd: "/a", label: "/a" }] });
  renderLanding(["claude"], { default_project: "/gone" });
  await screen.findByRole("option", { name: "/a" });
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("/a");
});

test("Set as default persists the selected project (#335 Phase 2)", async () => {
  mockProjects.mockResolvedValue({ projects: [{ cwd: "/a", label: "/a" }] });
  renderLanding(["claude"]);
  await screen.findByRole("option", { name: "/a" });
  // accessible name comes from the aria-label (verbose for screen readers)
  await userEvent.click(screen.getByRole("button", { name: /set the selected project as the default/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({ default_project: "/a" });
  // after saving it reflects the default state (aria-label flips)
  expect(screen.getByRole("button", { name: /this is your default project/i })).toBeDisabled();
});

test("create-folder makes a dir under a root and selects it (#335 Phase 3)", async () => {
  mockProjects.mockResolvedValue({ projects: [{ cwd: "/code/a", label: "/code/a" }] });
  vi.mocked(api.mkdir).mockResolvedValue({ cwd: "/code/newproj" });
  renderLanding(["claude"], { project_roots: ["/code"] });
  await screen.findByRole("option", { name: "/code/a" });
  await userEvent.click(screen.getByRole("button", { name: /new folder/i }));
  await userEvent.type(screen.getByLabelText(/new folder name/i), "newproj");
  await userEvent.click(screen.getByRole("button", { name: /^create$/i }));
  expect(api.mkdir).toHaveBeenCalledWith("/code", "newproj");
  // the new (not-yet-pickable) dir becomes selectable + is selected
  await screen.findByRole("option", { name: "/code/newproj" });
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("/code/newproj");
});

test("no New folder control when no roots are configured (#335 Phase 3)", async () => {
  mockProjects.mockResolvedValue({ projects: [{ cwd: "/code/a", label: "/code/a" }] });
  renderLanding(["claude"]);
  await screen.findByRole("option", { name: "/code/a" });
  expect(screen.queryByRole("button", { name: /new folder/i })).toBeNull();
});
