import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigReloadCtx } from "../../app/config";
import { api } from "../../lib/api";
import { structuredModelsStore } from "../../lib/structuredModelsStore";
import type { AppConfig, StructuredModelList } from "../../types/api";
import { SessionDefaults } from "./SessionDefaults";

vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  return {
    ...real,
    api: { ...real.api, structuredModels: vi.fn(), setAgentDefaults: vi.fn() },
  };
});

const LIST: StructuredModelList = {
  status: "ok",
  reason: null,
  models: [
    { id: "local/coder", label: "Coder", description: null, efforts: ["low", "high"], is_default: false },
    { id: "openai/plain", label: "Plain", description: null, efforts: [], is_default: false },
  ],
};

function renderIt(
  saved?: { model: string; effort: string | null },
  refresh: () => Promise<boolean> = vi.fn(async () => true),
) {
  const config = {
    csrf: "x",
    new_session_engines: [],
    terminal_backend: "ws",
    agent_defaults: {
      default_engine: null,
      bypass: true,
      session_defaults: saved ? { "opencode-api": saved } : {},
    },
  } as AppConfig;
  render(
    <ConfigCtx.Provider value={config}>
      <ConfigReloadCtx.Provider value={refresh}>
        <SessionDefaults engine="opencode-api" label="opencode — API" source="opencode" />
      </ConfigReloadCtx.Provider>
    </ConfigCtx.Provider>,
  );
  return refresh;
}

beforeEach(() => {
  vi.mocked(api.structuredModels).mockReset().mockResolvedValue(LIST);
  structuredModelsStore.reset(); // the model lists outlive a render too (#1503)
  vi.mocked(api.setAgentDefaults).mockReset().mockResolvedValue({} as never);
});

test("saves a model and one of ITS levels, then refreshes the config the new-session screen reads", async () => {
  const user = userEvent.setup();
  const refresh = renderIt();
  const model = await screen.findByTestId("session-defaults-model");
  const effort = await screen.findByTestId("session-defaults-effort");
  expect(effort).toBeDisabled(); // a level needs a model
  await user.selectOptions(model, "local/coder");
  await user.selectOptions(effort, "high");
  await user.click(screen.getByRole("button", { name: "Save defaults" }));
  await vi.waitFor(() =>
    expect(api.setAgentDefaults).toHaveBeenCalledWith({
      session_defaults: { "opencode-api": { model: "local/coder", effort: "high" } },
    }),
  );
  expect(refresh).toHaveBeenCalled();
  expect(await screen.findByTestId("session-defaults-status")).toHaveTextContent("Saved");
});

test("switching to a model without levels drops the level; saving the client's default clears", async () => {
  const user = userEvent.setup();
  renderIt({ model: "local/coder", effort: "high" });
  const model = await screen.findByTestId("session-defaults-model");
  await vi.waitFor(() => expect(model).toHaveValue("local/coder"));
  expect(screen.getByTestId("session-defaults-effort")).toHaveValue("high");
  await user.selectOptions(model, "openai/plain");
  expect(screen.getByTestId("session-defaults-effort")).toHaveValue("default");
  await user.selectOptions(model, "default");
  await user.click(screen.getByRole("button", { name: "Save defaults" }));
  await vi.waitFor(() =>
    expect(api.setAgentDefaults).toHaveBeenCalledWith({ session_defaults: { "opencode-api": null } }),
  );
});

test("a dropped model and a dropped level are named, not preselected", async () => {
  renderIt({ model: "gone/x", effort: null });
  expect(await screen.findByTestId("session-defaults-stale")).toHaveTextContent(
    "Saved model gone/x is no longer offered by opencode",
  );
  expect(screen.getByTestId("session-defaults-model")).toHaveValue("default");
});

test("an effort the model no longer offers keeps the model and names the level", async () => {
  renderIt({ model: "local/coder", effort: "max" });
  expect(await screen.findByTestId("session-defaults-stale")).toHaveTextContent(
    "Saved reasoning effort max is no longer offered",
  );
  expect(screen.getByTestId("session-defaults-model")).toHaveValue("local/coder");
  expect(screen.getByTestId("session-defaults-effort")).toHaveValue("default");
});

test("an unreadable list says why and Retry asks again", async () => {
  const user = userEvent.setup();
  vi.mocked(api.structuredModels)
    .mockResolvedValueOnce({ status: "unavailable", models: [], reason: "opencode exited" })
    .mockResolvedValueOnce(LIST);
  renderIt();
  expect(await screen.findByTestId("session-defaults-unavailable")).toHaveTextContent(
    "opencode exited",
  );
  await user.click(screen.getByRole("button", { name: "Retry" }));
  await screen.findByTestId("session-defaults-effort");
  expect(api.structuredModels).toHaveBeenCalledTimes(2);
});


test("Saved is announced only once the app reads the new defaults; otherwise it says so", async () => {
  const user = userEvent.setup();
  const refresh = vi.fn(async () => false);
  renderIt(undefined, refresh);
  await user.selectOptions(await screen.findByTestId("session-defaults-model"), "local/coder");
  await user.click(screen.getByRole("button", { name: "Save defaults" }));
  expect(await screen.findByTestId("session-defaults-status")).toHaveTextContent(
    "couldn’t reload your settings yet",
  );
  expect(refresh).toHaveBeenCalledTimes(2); // one retry
});


test("a saved default can be cleared while the model list is unavailable (#1412 review)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.structuredModels).mockResolvedValue({ status: "unavailable", models: [], reason: "opencode exited" });
  const refresh = vi.fn(async () => true);
  renderIt({ model: "local/coder", effort: "high" }, refresh);
  await screen.findByTestId("session-defaults-unavailable");
  expect(screen.getByRole("button", { name: "Save defaults" })).toBeDisabled();
  await user.click(screen.getByTestId("session-defaults-clear"));
  await vi.waitFor(() =>
    expect(api.setAgentDefaults).toHaveBeenCalledWith({ session_defaults: { "opencode-api": null } }),
  );
  expect(refresh).toHaveBeenCalled();
  expect(await screen.findByTestId("session-defaults-status")).toHaveTextContent("Cleared");
});

function tree(engine: string, refresh: () => Promise<boolean> = vi.fn(async () => true)) {
  const config = {
    csrf: "x",
    new_session_engines: [],
    terminal_backend: "ws",
    agent_defaults: { default_engine: null, bypass: true, session_defaults: {} },
  } as AppConfig;
  return (
    <ConfigCtx.Provider value={config}>
      <ConfigReloadCtx.Provider value={refresh}>
        <SessionDefaults engine={engine} label={engine} source="the client" />
      </ConfigReloadCtx.Provider>
    </ConfigCtx.Provider>
  );
}

test("an unsaved choice never follows the operator to another client's page (#1412 review)", async () => {
  const user = userEvent.setup();
  const { rerender } = render(tree("opencode-api"));
  await user.selectOptions(await screen.findByTestId("session-defaults-model"), "local/coder");
  expect(screen.getByRole("button", { name: "Save defaults" })).toBeEnabled();
  // Same component, another client (what an in-place route change does).
  rerender(tree("codex-api"));
  await screen.findByTestId("session-defaults-effort");
  expect(screen.getByTestId("session-defaults-model")).toHaveValue("default");
  expect(screen.getByRole("button", { name: "Save defaults" })).toBeDisabled();
  // A choice made HERE is what is sent, keyed to this client.
  await user.selectOptions(screen.getByTestId("session-defaults-model"), "openai/plain");
  await user.click(screen.getByRole("button", { name: "Save defaults" }));
  await vi.waitFor(() => expect(api.setAgentDefaults).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.setAgentDefaults).mock.calls[0][0]).toEqual({
    session_defaults: { "codex-api": { model: "openai/plain", effort: null } },
  });
});

test("a save that finishes after switching clients is not announced on the new client's page", async () => {
  const user = userEvent.setup();
  let finish: () => void = () => {};
  vi.mocked(api.setAgentDefaults).mockReturnValue(new Promise((res) => (finish = () => res({} as never))));
  const { rerender } = render(tree("opencode-api"));
  await user.selectOptions(await screen.findByTestId("session-defaults-model"), "local/coder");
  await user.click(screen.getByRole("button", { name: "Save defaults" }));
  rerender(tree("codex-api"));
  await act(async () => finish());
  await act(async () => {});
  expect(screen.queryByTestId("session-defaults-status")).toBeNull();
});

test("the agent page paints the New session form's cached list at once (#1503)", async () => {
  await structuredModelsStore.revalidate("opencode-api"); // an earlier visit, or the prefetch
  vi.mocked(api.structuredModels).mockReturnValue(new Promise(() => {})); // revalidation hangs
  renderIt({ model: "local/coder", effort: "high" });
  const model = screen.getByTestId("session-defaults-model");
  expect(model).toBeEnabled();
  expect(model).toHaveValue("local/coder");
  expect(screen.getByTestId("session-defaults-effort")).toHaveValue("high");
  expect(screen.queryByText(/Asking opencode/)).toBeNull();
});

test("a draft the revalidated list dropped stays shown as what Save sends (#1503 PR review)", async () => {
  const user = userEvent.setup();
  renderIt();
  const model = await screen.findByTestId("session-defaults-model");
  await vi.waitFor(() => expect(model).toBeEnabled());
  await user.selectOptions(model, "local/coder");
  await user.selectOptions(screen.getByTestId("session-defaults-effort"), "high");
  // A background revalidation lands a list without the drafted model.
  vi.mocked(api.structuredModels).mockResolvedValue({ ...LIST, models: [LIST.models[1]] });
  await act(() => structuredModelsStore.revalidate("opencode-api", { force: true }));
  expect(model).toHaveValue("local/coder");
  expect(screen.getByRole("option", { name: "local/coder (not offered now)" })).toBeInTheDocument();
  const effort = screen.getByTestId("session-defaults-effort");
  expect(effort).toHaveValue("high");
  expect(screen.getByRole("option", { name: "high (not offered now)" })).toBeInTheDocument();
  expect(effort).toBeEnabled(); // the dropped level can still be changed
  await user.click(screen.getByRole("button", { name: "Save defaults" }));
  await vi.waitFor(() =>
    expect(api.setAgentDefaults).toHaveBeenCalledWith({
      session_defaults: { "opencode-api": { model: "local/coder", effort: "high" } },
    }),
  );
});
