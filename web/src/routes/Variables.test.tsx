import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../lib/api";
import type { SharedVariable, SharedVariablesResponse } from "../types/variables";
import Variables from "./Variables";

vi.mock("../lib/api", async () => ({
  ...await vi.importActual<typeof import("../lib/api")>("../lib/api"),
  api: Object.fromEntries(["variables", "enableSharedVariables", "createVariable", "updateVariable", "deleteVariable", "createVariableCollection", "updateVariableCollection", "deleteVariableCollection"].map(k => [k, vi.fn()])),
}));
const mocked = vi.mocked(api);
const variable = (patch: Partial<SharedVariable> = {}): SharedVariable => ({ id: "id-host", name: "host", kind: "text", definition: { value_type: "text" }, set: true, value: "a.test", created_at: 1, updated_at: 10, ...patch });
let data: SharedVariablesResponse;
beforeEach(() => {
  vi.resetAllMocks();
  data = { variables: [variable()], collections: [], migration_required: false, limits: { variables_max: 100, value_max: 2000, name_max: 32, secret_min: 8, collections_max: 50 } };
  mocked.variables.mockImplementation(async () => structuredClone(data));
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
