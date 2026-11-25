import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes, useNavigate, type NavigateFunction } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ApiError, api } from "../lib/api";
import { MCP_SERVER_ROUTE, mcpServerPath } from "../lib/routes";
import type { McpServerDetail, McpSetting } from "../types/mcp";
import McpServer from "./McpServer";

/* Hermes on #2189: the server page is fenced to the server in the URL (a late answer for the
 * server you left never replaces the one you are on, nor strands it on "isn't here"), and a save
 * that stored a credential and then failed can simply be saved again. */

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      mcpServer: vi.fn(),
      projectEntities: vi.fn(),
      variables: vi.fn(),
      createVariable: vi.fn(),
      updateMcpServer: vi.fn(),
    },
  };
});
const m = vi.mocked(api);

const A = "a".repeat(16);
const B = "b".repeat(16);

function detail(id: string, label: string, settings: McpSetting[] = []): McpServerDetail {
  return {
    server: {
      id,
      name: label.toLowerCase(),
      label,
      source: { kind: "remote", url: "https://mcp.example.com/mcp" },
      transport: "http",
      args: [],
      settings,
      project_settings: {},
      projects: [],
      probe: null,
      enabled: true,
      rev: 1,
      installed: null,
      created_at: 1,
      updated_at: 1,
      tool_count: 0,
      enabled_projects: [],
      project_count: 0,
      state: { kind: "not_enabled" },
    },
    status: { projects: [], shared: { missing: [], check: "not_run" } },
    checks: [],
  };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

let navigate: NavigateFunction;
function Nav() {
  const nav = useNavigate();
  useEffect(() => {
    navigate = nav;
  }, [nav]);
  return null;
}

function mount(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Nav />
      <Routes>
        <Route path={MCP_SERVER_ROUTE} element={<McpServer />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  m.projectEntities.mockResolvedValue({ projects: [] } as never);
  m.variables.mockResolvedValue({ variables: [] } as never);
});
afterEach(() => vi.clearAllMocks());

describe("the server page belongs to the server in the URL", () => {
  test("a late answer for the server you left never replaces the one you are on", async () => {
    const slowA = deferred<McpServerDetail>();
    m.mcpServer.mockImplementation((id: string) =>
      id === A ? slowA.promise : Promise.resolve(detail(B, "Beta")),
    );
    mount(mcpServerPath(A));
    act(() => navigate(mcpServerPath(B)));
    expect(await screen.findByRole("heading", { name: "Beta" })).toBeTruthy();
    await act(async () => slowA.resolve(detail(A, "Alpha")));
    expect(screen.getByRole("heading", { name: "Beta" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Alpha" })).toBeNull();
  });

  test("a late 404 for the server you left never strands the one you are on", async () => {
    const slowA = deferred<McpServerDetail>();
    m.mcpServer.mockImplementation((id: string) =>
      id === A ? slowA.promise : Promise.resolve(detail(B, "Beta")),
    );
    mount(mcpServerPath(A));
    act(() => navigate(mcpServerPath(B)));
    expect(await screen.findByRole("heading", { name: "Beta" })).toBeTruthy();
    await act(async () => slowA.reject(new ApiError(404, "not found")));
    expect(screen.getByRole("heading", { name: "Beta" })).toBeTruthy();
    expect(screen.queryByTestId("mcp-server-missing")).toBeNull();
  });
});

describe("a save that stored a credential and then failed", () => {
  test("is saved again by the same button: the stored name is known, the PATCH is retried", async () => {
    const credential: McpSetting = {
      key: "Authorization",
      deliver: "header",
      kind: "credential",
      variable: "api_key",
      prefix: "Bearer ",
    };
    m.mcpServer.mockResolvedValue(detail(A, "Alpha", [credential]));
    m.createVariable.mockResolvedValue({ id: "v1", name: "api_key" } as never);
    m.updateMcpServer
      .mockRejectedValueOnce(new ApiError(503, "The server is busy"))
      .mockResolvedValueOnce(detail(A, "Alpha", [credential]).server as never);
    const user = userEvent.setup();
    mount(`${mcpServerPath(A)}?tab=settings`);
    const source = await screen.findByLabelText("Credential source for Authorization");
    await user.selectOptions(source, "new");
    await user.type(screen.getByLabelText("New credential value for Authorization"), "s3cret-value-1");
    await user.click(screen.getByTestId("mcp-settings-save"));
    await waitFor(() => expect(m.updateMcpServer).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/The server is busy/)).toBeTruthy();
    await user.click(screen.getByTestId("mcp-settings-save"));
    await waitFor(() => expect(m.updateMcpServer).toHaveBeenCalledTimes(2));
    expect(m.createVariable).toHaveBeenCalledTimes(1); // stored once, never twice
    expect(screen.queryByText(/No secret of that name/)).toBeNull();
  });
});
