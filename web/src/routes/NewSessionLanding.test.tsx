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
  return { ...actual, api: { projects: vi.fn() } };
});
const mockProjects = vi.mocked(api.projects);

function renderLanding(engines = ["claude"]) {
  const config: AppConfig = { csrf: "x", new_session_engines: engines, terminal_backend: "ws" };
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
