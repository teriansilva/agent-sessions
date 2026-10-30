import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../lib/api";
import type { SharedVariable, SharedVariablesResponse } from "../types/variables";
import Variables from "./Variables";

vi.mock("../lib/api", async () => ({
  ...await vi.importActual<typeof import("../lib/api")>("../lib/api"),
  api: Object.fromEntries(["variables", "enableSharedVariables", "createVariable", "updateVariable", "deleteVariable", "createVariableCollection", "updateVariableCollection", "deleteVariableCollection", "projectPickerEntities", "putVariableFile", "setVariableProjects", "variableAccessLog"].map(k => [k, vi.fn()])),
}));
const mocked = vi.mocked(api);
const variable = (patch: Partial<SharedVariable> = {}): SharedVariable => ({ id: "id-host", name: "host", kind: "text", definition: { value_type: "text" }, set: true, value: "a.test", created_at: 1, updated_at: 10, ...patch });
let data: SharedVariablesResponse;
beforeEach(() => {
  vi.resetAllMocks();
  data = { variables: [variable()], collections: [], migration_required: false, limits: { variables_max: 100, value_max: 2000, name_max: 32, secret_min: 8, collections_max: 50 } };
  mocked.variables.mockImplementation(async () => structuredClone(data));
  mocked.projectPickerEntities.mockResolvedValue({ projects: [{ id: "atlas", name: "Atlas iOS" }, { id: "beta", name: "Beta" }] } as never);
  mocked.variableAccessLog.mockResolvedValue({ entries: [] });
});
function show(state?: unknown) {
  const router = createMemoryRouter([
    { path: "/library/variables", element: <Variables /> },
    { path: "/elsewhere", element: <p>Elsewhere</p> },
    { path: "/templates/:id", element: <p>Template editor</p> },
  ], { initialEntries: [{ pathname: "/library/variables", state }] });
  render(<RouterProvider router={router} />);
  return router;
}
const save = () => userEvent.click(screen.getByRole("button", { name: "Save", exact: true }));
async function edit(name = "host") { await userEvent.click(await screen.findByRole("button", { name: `Edit ${name}`, exact: true })); }

test("GET stays read-only and migration must be explicitly enabled", async () => {
  data.migration_required = true; data.variables[0].id = null;
  mocked.enableSharedVariables.mockImplementation(async () => { data.migration_required = false; data.variables[0].id = "id-host"; return data; });
  show();
  expect(await screen.findByRole("button", { name: "Edit host" })).toBeDisabled();
  expect(mocked.enableSharedVariables).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Enable shared variables" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Edit host" })).toBeEnabled());
  expect(mocked.enableSharedVariables).toHaveBeenCalledTimes(1);
});

test("failed loading has a retry and keeps create actions disabled", async () => {
  mocked.variables.mockRejectedValueOnce(new ApiError(503, "the store cannot be read in full"));
  show();
  expect(await screen.findByRole("alert")).toHaveTextContent("cannot be read in full");
  expect(screen.getByRole("button", { name: "New variable" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByRole("button", { name: "Edit host" })).toBeEnabled();
});

test("empty library creates a multiline value, with invalid names refused before sending", async () => {
  data.variables = [];
  mocked.createVariable.mockImplementation(async input => { const row = variable({ ...input, id: "id-run" }); data.variables.push(row); return row; });
  show();
  expect(await screen.findByText(/No variables here yet/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "New variable" }));
  await userEvent.type(screen.getByLabelText("Name", { exact: true }), "9lives");
  await save();
  expect(screen.getByRole("alert")).toHaveTextContent("starting with a letter");
  expect(screen.getByRole("alert")).toHaveFocus();
  expect(mocked.createVariable).not.toHaveBeenCalled();
  await userEvent.clear(screen.getByLabelText("Name", { exact: true }));
  await userEvent.type(screen.getByLabelText("Name", { exact: true }), "run");
  await userEvent.type(screen.getByLabelText("Global value", { exact: true }), "cd repo{Enter}npm test");
  expect(mocked.createVariable).not.toHaveBeenCalled();
  await save();
  expect(mocked.createVariable).toHaveBeenCalledWith(expect.objectContaining({ name: "run", value: "cd repo\nnpm test", kind: "text" }));
  await edit("run");
  expect(screen.getByLabelText("Global value", { exact: true })).toHaveValue("cd repo\nnpm test");
});

test("stale writes keep the draft and original fence until explicit reload", async () => {
  mocked.updateVariable.mockRejectedValue(new ApiError(409, "Changed elsewhere", { current: variable({ value: "theirs", updated_at: 20 }) }));
  show(); await edit();
  const input = screen.getByLabelText("Global value", { exact: true });
  await userEvent.clear(input); await userEvent.type(input, "mine");
  data.variables[0] = variable({ value: "theirs", updated_at: 20 });
  await save();
  expect(mocked.updateVariable).toHaveBeenLastCalledWith("id-host", expect.objectContaining({ value: "mine" }), 10);
  expect(input).toHaveValue("mine");
  expect(mocked.variables).toHaveBeenCalledTimes(1);
  await save();
  expect(mocked.updateVariable).toHaveBeenLastCalledWith("id-host", expect.objectContaining({ value: "mine" }), 10);
  await userEvent.click(screen.getByRole("button", { name: "Discard draft & reload current" }));
  await waitFor(() => expect(screen.getByLabelText("Global value", { exact: true })).toHaveValue("theirs"));
  await save();
  expect(mocked.updateVariable).toHaveBeenLastCalledWith("id-host", expect.anything(), 20);
});

test("a pending save freezes input and duplicate submits while preserving the starting revision", async () => {
  let settle!: (row: SharedVariable) => void;
  mocked.updateVariable.mockImplementation(() => new Promise(resolve => { settle = resolve; }));
  show(); await edit();
  const input = screen.getByLabelText("Global value", { exact: true });
  await userEvent.clear(input); await userEvent.type(input, "first");
  await userEvent.keyboard("{Control>}{Enter}{/Control}");
  expect(input).toBeDisabled();
  expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
  await userEvent.type(input, "second");
  await userEvent.keyboard("{Control>}{Enter}{/Control}");
  expect(input).toHaveValue("first");
  expect(mocked.updateVariable).toHaveBeenCalledTimes(1);
  await act(async () => settle(variable({ value: "first", updated_at: 11 })));
});

test("delete refusal names dependent templates and never pretends it is an edit conflict", async () => {
  mocked.deleteVariable.mockRejectedValue(new ApiError(409, "host is still used by 2 templates", { dependants: [{ id: "smoke", name: "Smoke test" }, { id: "logs", name: "Tail logs" }] }));
  show(); await edit();
  await userEvent.click(screen.getByRole("button", { name: "Delete variable" }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete variable" }));
  const alert = await screen.findByRole("alert");
  expect(within(alert).getByRole("link", { name: "Tail logs" })).toHaveAttribute("href", "/templates/logs");
  expect(mocked.deleteVariable).toHaveBeenCalledWith("id-host", 10);
  expect(screen.queryByRole("button", { name: "Discard draft & reload current" })).toBeNull();
  expect(screen.getByRole("dialog")).toBeInTheDocument();
});

test("unreadable inventory refusal retains the item and the server's reason", async () => {
  mocked.deleteVariable.mockRejectedValue(new ApiError(503, "the template library could not be read in full"));
  show(); await edit();
  await userEvent.click(screen.getByRole("button", { name: "Delete variable" }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete variable" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("could not be read in full");
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
  expect(screen.getByLabelText("Global value", { exact: true })).toHaveValue("a.test");
});

test("secret input enforces the minimum and never returns the saved value", async () => {
  data.variables = [];
  mocked.createVariable.mockImplementation(async input => { const row = variable({ name: input.name, kind: "secret", value: undefined }); data.variables.push(row); return row; });
  show(); await screen.findByText(/No variables here yet/);
  await userEvent.click(screen.getByRole("button", { name: "New secret" }));
  await userEvent.type(screen.getByLabelText("Name", { exact: true }), "db_pass");
  const input = screen.getByLabelText("Global value", { exact: true });
  expect(input).toHaveAttribute("type", "password");
  await userEvent.type(input, "short"); await save();
  expect(screen.getByRole("alert")).toHaveTextContent("at least 8 characters");
  expect(mocked.createVariable).not.toHaveBeenCalled();
  await userEvent.type(input, "-and-longer"); await save();
  expect(mocked.createVariable).toHaveBeenCalledWith(expect.objectContaining({ name: "db_pass", kind: "secret", value: "short-and-longer" }));
  expect(await screen.findByRole("article", { name: "db_pass" })).toHaveTextContent("Stored · hidden");
  expect(document.body.textContent).not.toContain("short-and-longer");
});

test("secret replacement starts empty; blank preserves, clearing is explicit, re-entry stays fenced", async () => {
  data.variables = [variable({ kind: "secret", value: undefined, needs_reentry: true, definition: { value_type: "text", pattern: "^[a-z]+$" } })];
  mocked.updateVariable.mockRejectedValue(new ApiError(503, "busy; retry"));
  show(); expect(await screen.findByText("Needs re-entry")).toBeInTheDocument(); await edit();
  const input = screen.getByLabelText("Replacement secret");
  expect(input).toHaveValue(""); expect(input).toHaveAttribute("type", "password");
  await save();
  const preserved = mocked.updateVariable.mock.calls.at(-1)![1];
  expect(preserved.definition).toHaveProperty("pattern", "^[a-z]+$");
  expect(preserved).not.toHaveProperty("value"); expect(preserved).not.toHaveProperty("clear_value");
  await userEvent.type(input, "a-brand-new-secret"); await save();
  expect(mocked.updateVariable).toHaveBeenLastCalledWith("id-host", expect.objectContaining({ value: "a-brand-new-secret" }), 10);
  await userEvent.click(screen.getByLabelText("Store a global value")); await save();
  const cleared = mocked.updateVariable.mock.calls.at(-1)![1];
  expect(cleared).toHaveProperty("clear_value", true); expect(cleared).not.toHaveProperty("value");
});

test("suggestion prefill is consumed once and leaving requires discarding the draft", async () => {
  const router = show({ variablePrefill: { name: "suggested", value: "initial", secret: false } });
  expect(await screen.findByLabelText("Name", { exact: true })).toHaveValue("suggested");
  expect(screen.getByLabelText("Global value", { exact: true })).toHaveValue("initial");
  await waitFor(() => expect(router.state.location.state).toBeNull());
  await act(async () => { void router.navigate("/elsewhere"); });
  expect(screen.getByRole("dialog", { name: "Discard your changes?" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Discard changes" }));
  expect(await screen.findByText("Elsewhere")).toBeInTheDocument();
  await act(async () => { await router.navigate(-1); });
  await userEvent.click(await screen.findByRole("button", { name: "New variable" }));
  expect(screen.getByLabelText("Name", { exact: true })).toHaveValue("");
  expect(screen.getByLabelText("Global value", { exact: true })).toHaveValue("");
});

test("a secret suggestion never prefills its value even if supplied in navigation state", async () => {
  show({ variablePrefill: { name: "secret_suggestion", value: "must-not-be-filled", secret: true } });
  expect(await screen.findByLabelText("Global value", { exact: true })).toHaveValue("");
  expect(document.body.textContent).not.toContain("must-not-be-filled");
});

test("Escape retains the draft unless its discard is confirmed", async () => {
  show(); await edit();
  await userEvent.type(screen.getByLabelText("Global value", { exact: true }), " changed");
  await userEvent.keyboard("{Escape}");
  expect(screen.getByRole("dialog", { name: "Discard your changes?" })).toBeInTheDocument();
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
  expect(screen.getByLabelText("Global value", { exact: true })).toHaveValue("a.test changed");
});

test("a secret file row (#1786) keeps its file name's case, shows its size and the AGENTS chip, and is editable", async () => {
  data.variables = [variable({ id: "id-key", name: "asc_api_key", kind: "secret_file", value: undefined, agent_access: true, file: { name: "AuthKey_7F3K.p8", size: 2150 }, projects: ["atlas"] })];
  show();
  const row = await screen.findByRole("article", { name: "asc_api_key" });
  expect(within(row).getByText("AuthKey_7F3K.p8")).toBeInTheDocument();
  expect(within(row).getByText("Stored · hidden · 2.1 KB")).toBeInTheDocument();
  expect(within(row).getByText("Agents")).toBeInTheDocument();
  expect(within(row).queryByText("undefined")).toBeNull();
  expect(within(row).getByRole("button", { name: "Edit asc_api_key" })).toBeEnabled();
});

const saved = (patch: Partial<SharedVariable>) => variable({ id: "id-new", kind: "secret_file", value: undefined, set: true, updated_at: 20, ...patch });

test("a new secret file is created, uploaded, given agent access and bound, in that order", async () => {
  mocked.createVariable.mockResolvedValue(saved({ name: "asc_api_key", set: false, updated_at: 20 }));
  mocked.putVariableFile.mockResolvedValue(saved({ name: "asc_api_key", updated_at: 21, file: { name: "AuthKey.p8", size: 9 } }));
  mocked.updateVariable.mockResolvedValue(saved({ name: "asc_api_key", updated_at: 22, agent_access: true }));
  mocked.setVariableProjects.mockResolvedValue(saved({ name: "asc_api_key", updated_at: 23, agent_access: true, projects: ["atlas"] }));
  show();
  await userEvent.click(await screen.findByRole("button", { name: "New secret" }));
  await userEvent.type(screen.getByLabelText("Name", { exact: true }), "asc_api_key");
  await userEvent.click(screen.getByRole("button", { name: "File · upload" }));
  const file = new File(["-----BEGIN"], "AuthKey.p8");
  await userEvent.upload(document.querySelector('input[type="file"]') as HTMLInputElement, file);
  expect(screen.getByText("AuthKey.p8")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("switch"));
  expect(screen.getByText(/No project binds this secret yet/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("checkbox", { name: "Atlas iOS" }));
  expect(screen.getByText(/Agents in Atlas iOS can ask the secrets tool/)).toBeInTheDocument();
  await save();
  await waitFor(() => expect(mocked.setVariableProjects).toHaveBeenCalled());
  expect(mocked.createVariable).toHaveBeenCalledWith(expect.objectContaining({ name: "asc_api_key", kind: "secret_file" }));
  expect(mocked.createVariable.mock.calls[0][0]).not.toHaveProperty("value");
  expect(mocked.putVariableFile).toHaveBeenCalledWith("id-new", file, "AuthKey.p8", 20);
  expect(mocked.updateVariable).toHaveBeenCalledWith("id-new", { agent_access: true }, 21);
  expect(mocked.setVariableProjects).toHaveBeenCalledWith("id-new", ["atlas"], 22);
});

test("a step that fails after the create leaves the editor on the saved row, so Save again edits it", async () => {
  mocked.createVariable.mockResolvedValue(saved({ name: "asc_api_key", set: false, updated_at: 20 }));
  mocked.putVariableFile.mockRejectedValueOnce(new ApiError(503, "busy")).mockResolvedValue(saved({ name: "asc_api_key", updated_at: 21, file: { name: "k.p8", size: 3 } }));
  mocked.updateVariable.mockResolvedValue(saved({ name: "asc_api_key", updated_at: 21 }));
  show();
  await userEvent.click(await screen.findByRole("button", { name: "New secret" }));
  await userEvent.type(screen.getByLabelText("Name", { exact: true }), "asc_api_key");
  await userEvent.click(screen.getByRole("button", { name: "File · upload" }));
  await userEvent.upload(document.querySelector('input[type="file"]') as HTMLInputElement, new File(["abc"], "k.p8"));
  await save();
  expect(await screen.findByRole("alert")).toHaveTextContent("busy");
  await save();
  await waitFor(() => expect(mocked.putVariableFile).toHaveBeenCalledTimes(2));
  expect(mocked.createVariable).toHaveBeenCalledTimes(1);
});

test("a retry after a later step fails does not upload the file again", async () => {
  mocked.createVariable.mockResolvedValue(saved({ name: "asc_api_key", set: false, updated_at: 20 }));
  mocked.putVariableFile.mockResolvedValue(saved({ name: "asc_api_key", updated_at: 21, file: { name: "k.p8", size: 3 } }));
  mocked.setVariableProjects.mockRejectedValueOnce(new ApiError(503, "busy")).mockResolvedValue(saved({ name: "asc_api_key", updated_at: 23, projects: ["atlas"] }));
  mocked.updateVariable.mockResolvedValue(saved({ name: "asc_api_key", updated_at: 22, file: { name: "k.p8", size: 3 } }));
  show();
  await userEvent.click(await screen.findByRole("button", { name: "New secret" }));
  await userEvent.type(screen.getByLabelText("Name", { exact: true }), "asc_api_key");
  await userEvent.click(screen.getByRole("button", { name: "File · upload" }));
  await userEvent.upload(document.querySelector('input[type="file"]') as HTMLInputElement, new File(["abc"], "k.p8"));
  await userEvent.click(await screen.findByRole("checkbox", { name: "Atlas iOS" }));
  await save();
  expect(await screen.findByRole("alert")).toHaveTextContent("busy");
  await save();
  await waitFor(() => expect(mocked.setVariableProjects).toHaveBeenCalledTimes(2));
  expect(mocked.putVariableFile).toHaveBeenCalledTimes(1);
  expect(mocked.createVariable).toHaveBeenCalledTimes(1);
});

test("an oversized or empty file is refused before anything is sent", async () => {
  show();
  await userEvent.click(await screen.findByRole("button", { name: "New secret" }));
  await userEvent.click(screen.getByRole("button", { name: "File · upload" }));
  const big = new File([new Uint8Array(256 * 1024 + 1)], "big.bin");
  await userEvent.upload(document.querySelector('input[type="file"]') as HTMLInputElement, big);
  expect(await screen.findByText(/a secret file can be at most 256.0 KB/)).toBeInTheDocument();
  await userEvent.upload(document.querySelector('input[type="file"]') as HTMLInputElement, new File([], "empty.p8"));
  expect(await screen.findByText(/That file is empty/)).toBeInTheDocument();
  expect(mocked.putVariableFile).not.toHaveBeenCalled();
});

test("a multi-line secret is sent without its surrounding blank lines", async () => {
  mocked.createVariable.mockResolvedValue(variable({ id: "id-pem", name: "deploy_key", kind: "secret", value: undefined }));
  show();
  await userEvent.click(await screen.findByRole("button", { name: "New secret" }));
  await userEvent.type(screen.getByLabelText("Name", { exact: true }), "deploy_key");
  await userEvent.click(screen.getByRole("button", { name: "Multi-line · PEM, key, JSON" }));
  const box = screen.getByLabelText("Global value", { exact: true });
  expect(box.tagName).toBe("TEXTAREA");
  await userEvent.type(box, "{Enter}-----BEGIN KEY-----{Enter}abcdefghij{Enter}");
  await save();
  await waitFor(() => expect(mocked.createVariable).toHaveBeenCalled());
  expect(mocked.createVariable.mock.calls[0][0]).toMatchObject({ kind: "secret", value: "-----BEGIN KEY-----\nabcdefghij" });
});

test("the access log shows deliveries and refusals, an empty log, and a log that cannot be read", async () => {
  data.variables = [variable({ id: "id-key", name: "asc_api_key", kind: "secret", value: undefined, agent_access: true, projects: ["atlas"] })];
  mocked.variableAccessLog.mockResolvedValueOnce({ entries: [
    { at: Date.now() / 1000, project_id: "atlas", engine: "claude", session_key: "claude:abc", outcome: "materialized", reason: null },
    { at: Date.now() / 1000, project_id: "beta", engine: "codex", session_key: "codex:def", outcome: "refused", reason: "access_off" },
  ] });
  show();
  await edit("asc_api_key");
  expect(await screen.findByRole("link", { name: /open session/ })).toHaveAttribute("href", "/s/claude/abc");
  expect(screen.getByText("refused · access off")).toBeInTheDocument();
  expect(screen.getAllByText("Atlas iOS").length).toBeGreaterThan(0);
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  mocked.variableAccessLog.mockResolvedValueOnce({ entries: [] });
  await edit("asc_api_key");
  expect(await screen.findByText("No agent has requested this secret yet.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  mocked.variableAccessLog.mockRejectedValueOnce(new ApiError(503, "no"));
  await edit("asc_api_key");
  expect(await screen.findByText(/The access log could not be read/)).toBeInTheDocument();
});

const storedFile = (patch: Partial<SharedVariable> = {}) => variable({ id: "id-key", name: "asc_api_key", kind: "secret_file", value: undefined, definition: { value_type: "text", label: "asc_api_key" }, agent_access: true, projects: ["atlas"], file: { name: "AuthKey.p8", size: 257 }, updated_at: 30, ...patch });

test("a projects-only change to a stored secret sends no edit, only the binding", async () => {
  data.variables = [storedFile()];
  mocked.setVariableProjects.mockResolvedValue(storedFile({ projects: ["atlas", "beta"], updated_at: 31 }));
  show();
  await edit("asc_api_key");
  await userEvent.click(await screen.findByRole("checkbox", { name: "Beta" }));
  await save();
  await waitFor(() => expect(mocked.setVariableProjects).toHaveBeenCalledWith("id-key", ["atlas", "beta"], 30));
  expect(mocked.updateVariable).not.toHaveBeenCalled();
  expect(mocked.putVariableFile).not.toHaveBeenCalled();
});

test("turning agent access off on a stored secret sends exactly that", async () => {
  data.variables = [storedFile()];
  mocked.updateVariable.mockResolvedValue(storedFile({ agent_access: false, updated_at: 31 }));
  show();
  await edit("asc_api_key");
  await userEvent.click(await screen.findByRole("switch"));
  await save();
  await waitFor(() => expect(mocked.updateVariable).toHaveBeenCalledTimes(1));
  expect(mocked.updateVariable.mock.calls[0][1]).toMatchObject({ agent_access: false });
  expect(mocked.setVariableProjects).not.toHaveBeenCalled();
});

test("a binding to a project the picker no longer lists can still be removed", async () => {
  data.variables = [storedFile({ projects: ["atlas", "gone-1"] })];
  mocked.setVariableProjects.mockResolvedValue(storedFile({ updated_at: 31 }));
  show();
  await edit("asc_api_key");
  const gone = await screen.findByRole("checkbox", { name: /gone-1 · deleted or archived project/ });
  expect(gone).toBeChecked();
  await userEvent.click(gone);
  await save();
  await waitFor(() => expect(mocked.setVariableProjects).toHaveBeenCalledWith("id-key", ["atlas"], 30));
});

test("a secret file offers no Require or Pattern controls", async () => {
  data.variables = [storedFile()];
  show();
  await edit("asc_api_key");
  await screen.findByRole("switch");
  expect(screen.queryByText("Require a value when used")).toBeNull();
  expect(screen.queryByText("Pattern (regular expression)")).toBeNull();
});

test("a refused file after a valid one leaves nothing queued: Save uploads neither", async () => {
  data.variables = [storedFile()];
  show();
  await edit("asc_api_key");
  const input = () => document.querySelector('input[type="file"]') as HTMLInputElement;
  await userEvent.upload(input(), new File(["valid-bytes"], "A.p8"));
  expect(await screen.findByText("A.p8")).toBeInTheDocument();
  await userEvent.upload(input(), new File([], "B.p8"));
  expect(await screen.findByText(/Nothing is queued to store/)).toBeInTheDocument();
  expect(screen.queryByText("A.p8")).toBeNull();
  await save();
  await waitFor(() => expect(screen.queryByRole("button", { name: "Save", exact: true })).toBeNull());
  expect(mocked.putVariableFile).not.toHaveBeenCalled();
});

test("the type cannot change while a new secret file is saving, and its file is still stored", async () => {
  let finish: (row: SharedVariable) => void = () => {};
  mocked.createVariable.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  mocked.putVariableFile.mockResolvedValue(saved({ name: "asc_api_key", updated_at: 21, file: { name: "k.p8", size: 3 } }));
  show();
  await userEvent.click(await screen.findByRole("button", { name: "New secret" }));
  await userEvent.type(screen.getByLabelText("Name", { exact: true }), "asc_api_key");
  await userEvent.click(screen.getByRole("button", { name: "File · upload" }));
  await userEvent.upload(document.querySelector('input[type="file"]') as HTMLInputElement, new File(["abc"], "k.p8"));
  await save();
  const single = screen.getByRole("button", { name: "Single line" });
  expect(single).toBeDisabled();
  await userEvent.click(single);
  finish(saved({ name: "asc_api_key", set: false, updated_at: 20 }));
  await waitFor(() => expect(mocked.putVariableFile).toHaveBeenCalledTimes(1));
  expect(mocked.putVariableFile.mock.calls[0][2]).toBe("k.p8");
});

test("a file chosen after a failed upload is the one the retry stores, under its own name", async () => {
  mocked.createVariable.mockResolvedValue(saved({ name: "asc_api_key", set: false, updated_at: 20 }));
  mocked.putVariableFile.mockRejectedValueOnce(new ApiError(503, "busy")).mockResolvedValue(saved({ name: "asc_api_key", updated_at: 21, file: { name: "second.p8", size: 6 } }));
  mocked.updateVariable.mockResolvedValue(saved({ name: "asc_api_key", updated_at: 20 }));
  show();
  await userEvent.click(await screen.findByRole("button", { name: "New secret" }));
  await userEvent.type(screen.getByLabelText("Name", { exact: true }), "asc_api_key");
  await userEvent.click(screen.getByRole("button", { name: "File · upload" }));
  const input = () => document.querySelector('input[type="file"]') as HTMLInputElement;
  await userEvent.upload(input(), new File(["first"], "first.p8"));
  await save();
  expect(await screen.findByRole("alert")).toHaveTextContent("busy");
  const second = new File(["second"], "second.p8");
  await userEvent.upload(input(), second);
  await save();
  await waitFor(() => expect(mocked.putVariableFile).toHaveBeenCalledTimes(2));
  const [, file, name] = mocked.putVariableFile.mock.calls[1];
  expect(file).toBe(second);
  expect(name).toBe("second.p8");
});
