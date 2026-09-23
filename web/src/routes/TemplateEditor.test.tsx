import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../lib/api";
import type { Template } from "../types/api";
import { defaultValues, renderTemplate } from "../lib/templateMessage";
import TemplateEditor from "./TemplateEditor";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      templates: vi.fn(),
      templateVariables: vi.fn(() => Promise.resolve({ variables: [], limits: {} })),
      createTemplate: vi.fn(),
      updateTemplate: vi.fn(),
      deleteTemplate: vi.fn(),
      upload: vi.fn(),
      uploadBlob: vi.fn(() => Promise.resolve(new Blob([new Uint8Array([137, 80, 78, 71])]))),
    },
  };
});

let urlCounter = 0;
beforeEach(() => {
  urlCounter = 0;
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => `blob:test/${++urlCounter}`),
  });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
});

const LIMITS = {
  templates_max: 200,
  name_max: 120,
  description_max: 300,
  tags_max: 8,
  body_max: 100_000,
  fields_max: 12,
  label_max: 60,
  default_max: 500,
  images_max: 8,
  image_suffixes: [".png", ".jpg"],
};

function tpl(over: Partial<Template> = {}): Template {
  return {
    id: "pr-review",
    name: "PR review checklist",
    description: "Review a PR.",
    tags: ["review"],
    body: "Review PR {{pr_url}} for {{issue_ref}} and {{ghost}}",
    fields: [
      { name: "pr_url", label: "PR link", default: "", required: true },
      { name: "issue_ref", label: "Issue", default: "the linked issue", required: false },
    ],
    images: [{ name: "shot.png", path: "/home/u/.agent-sessions/uploads/20260903-1-shot.png" }],
    created_at: 1_788_400_000,
    updated_at: 1_788_430_000.5,
    used_count: 0,
    last_used_at: null,
    ...over,
  };
}

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

// A DATA router, as the app has (#907 review): `useBlocker` needs one, and Back/Forward and
// arbitrary in-app navigations can be driven through `router.navigate`.
function makeRouter(entries: (string | { pathname: string; state?: unknown })[], index?: number) {
  return createMemoryRouter(
    [
      { path: "/templates", element: <p>gallery route</p> },
      { path: "/templates/new", element: <TemplateEditor /> },
      { path: "/templates/:id", element: <TemplateEditor /> },
      { path: "/settings", element: <p>settings route</p> },
    ],
    { initialEntries: entries, initialIndex: index ?? entries.length - 1 },
  );
}

function renderEditor(path: string) {
  const router = makeRouter([path]);
  const utils = render(<RouterProvider router={router} />);
  return { ...utils, router };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocked.templates.mockResolvedValue({ templates: [tpl()], limits: LIMITS });
});

test("the preview is the exact paste: defaults substituted, empty slots kept, paths appended, unknown tokens flagged", async () => {
  renderEditor("/templates/pr-review");
  const pre = await screen.findByLabelText(/what the agent receives/i);
  // Raw textContent, not the whitespace-normalizing matcher: the preview IS the paste.
  expect(pre.textContent).toBe(
    "Review PR {{pr_url}} for the linked issue and {{ghost}} /home/u/.agent-sessions/uploads/20260903-1-shot.png",
  );
  expect(pre.textContent).toBe(renderTemplate(tpl(), defaultValues(tpl().fields)));
  expect(screen.getByText(/\{\{ghost\}\} names no field and is sent literally/i)).toBeInTheDocument();
  // The thumbnail in the images row is fetched through the seam and shown as an object URL.
  expect(await screen.findByRole("img", { name: "shot.png" })).toHaveAttribute("src", "blob:test/1");
  expect(mocked.uploadBlob).toHaveBeenCalledWith("/home/u/.agent-sessions/uploads/20260903-1-shot.png", expect.anything());
});

test("a new template cannot be saved until it has a name and instructions; then it POSTs and returns to the gallery", async () => {
  mocked.createTemplate.mockResolvedValue(tpl({ id: "deploy-watch", name: "Deploy watch" }));
  renderEditor("/templates/new");
  const save = await screen.findByRole("button", { name: /^save$/i });
  expect(save).toBeDisabled();
  await userEvent.type(screen.getByLabelText(/^name/i), "Deploy watch");
  expect(save).toBeDisabled();
  await userEvent.click(screen.getByLabelText(/instructions/i));
  await userEvent.paste("Watch the deploy for {{sha}}"); // braces are user-event key syntax

  expect(save).toBeEnabled();
  // A field row: name + required.
  await userEvent.click(screen.getByRole("button", { name: /add field/i }));
  await userEvent.type(screen.getByLabelText(/field 1 name/i), "sha");
  await userEvent.click(screen.getByLabelText(/field 1 required/i));
  // A tag via Enter.
  await userEvent.type(screen.getByLabelText(/add a tag/i), "ops{Enter}");
  expect(screen.getByRole("button", { name: /remove tag ops/i })).toBeInTheDocument();
  await userEvent.click(save);
  await waitFor(() => expect(mocked.createTemplate).toHaveBeenCalledTimes(1));
  expect(mocked.createTemplate.mock.calls[0][0]).toEqual({
    name: "Deploy watch",
    description: "",
    tags: ["ops"],
    body: "Watch the deploy for {{sha}}",
    fields: [
      { name: "sha", label: "", default: "", required: true, source: "template", kind: "text" },
    ],
    images: [],
  });
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
});

test("saving an existing template sends the updated_at it was loaded with as the fence", async () => {
  mocked.updateTemplate.mockResolvedValue(tpl({ name: "Renamed", updated_at: 1_788_440_000 }));
  renderEditor("/templates/pr-review");
  const name = await screen.findByLabelText(/^name/i);
  expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled(); // nothing changed yet
  await userEvent.clear(name);
  await userEvent.type(name, "Renamed");
  expect(screen.getByText(/unsaved/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  await waitFor(() => expect(mocked.updateTemplate).toHaveBeenCalledTimes(1));
  const [id, input, expected] = mocked.updateTemplate.mock.calls[0];
  expect(id).toBe("pr-review");
  expect(input.name).toBe("Renamed");
  expect(expected).toBe(1_788_430_000.5);
});

test("a 409 offers reload-or-overwrite; overwrite retries against the current revision", async () => {
  const theirs = tpl({ name: "Their name", updated_at: 1_788_450_000 });
  mocked.updateTemplate
    .mockRejectedValueOnce(new ApiError(409, "changed", { current: theirs }))
    .mockResolvedValueOnce(tpl({ name: "Mine", updated_at: 1_788_460_000 }));
  renderEditor("/templates/pr-review");
  const name = await screen.findByLabelText(/^name/i);
  await userEvent.clear(name);
  await userEvent.type(name, "Mine");
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/saved from another tab or device/i)).toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole("button", { name: /overwrite/i }));
  await waitFor(() => expect(mocked.updateTemplate).toHaveBeenCalledTimes(2));
  expect(mocked.updateTemplate.mock.calls[1][2]).toBe(1_788_450_000);
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
});

test("a 409 → reload theirs replaces the form and drops the edits", async () => {
  const theirs = tpl({ name: "Their name", body: "their body", updated_at: 1_788_450_000 });
  mocked.updateTemplate.mockRejectedValueOnce(new ApiError(409, "changed", { current: theirs }));
  renderEditor("/templates/pr-review");
  const name = await screen.findByLabelText(/^name/i);
  await userEvent.clear(name);
  await userEvent.type(name, "Mine");
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("button", { name: /reload theirs/i }));
  expect(screen.getByLabelText(/^name/i)).toHaveValue("Their name");
  expect(screen.getByLabelText(/instructions/i)).toHaveValue("their body");
  expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
  expect(screen.getByText(/your edits were dropped/i)).toBeInTheDocument();
});

test("cancel with unsaved edits asks first; keep editing stays, discard leaves", async () => {
  renderEditor("/templates/pr-review");
  const name = await screen.findByLabelText(/^name/i);
  await userEvent.type(name, "!");
  await userEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
  let dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/unsaved changes/i)).toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole("button", { name: /keep editing/i }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByLabelText(/^name/i)).toHaveValue("PR review checklist!");
  await userEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
  dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("button", { name: /discard and leave/i }));
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
});

test("adding an image uploads it and appends the returned path to the preview", async () => {
  mocked.upload.mockResolvedValue({
    path: "/home/u/.agent-sessions/uploads/20260903-2-ref.png",
    name: "ref.png",
    stored: "20260903-2-ref.png",
  });
  renderEditor("/templates/pr-review");
  await screen.findByLabelText(/what the agent receives/i);
  const file = new File([new Uint8Array([137, 80, 78, 71])], "ref.png", { type: "image/png" });
  await userEvent.upload(screen.getByLabelText(/choose images/i), file);
  await waitFor(() => expect(mocked.upload).toHaveBeenCalledWith(file));
  expect(await screen.findByRole("img", { name: "ref.png" })).toHaveAttribute("src", "blob:test/2");
  expect(screen.getByLabelText(/what the agent receives/i)).toHaveTextContent(
    /20260903-1-shot\.png \/home\/u\/\.agent-sessions\/uploads\/20260903-2-ref\.png$/,
  );
  expect(screen.getByText(/unsaved/i)).toBeInTheDocument();
});

test("delete from the editor is fenced and lands on the gallery", async () => {
  mocked.deleteTemplate.mockResolvedValue(undefined);
  renderEditor("/templates/pr-review");
  await userEvent.click(await screen.findByRole("button", { name: /delete template/i }));
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));
  await waitFor(() => expect(mocked.deleteTemplate).toHaveBeenCalledWith("pr-review", 1_788_430_000.5));
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
});

test("an unknown id is a missing state, not a blank editor", async () => {
  renderEditor("/templates/nope");
  expect(await screen.findByText(/no such template/i)).toBeInTheDocument();
});

test("the router blocker holds Back and any in-app navigation on a dirty editor; keep stays, discard goes (#907 review)", async () => {
  const router = makeRouter(["/templates", "/templates/pr-review"], 1);
  render(<RouterProvider router={router} />);
  const name = await screen.findByLabelText(/^name/i);
  await userEvent.type(name, "!");
  // A topbar-style navigation elsewhere.
  await act(async () => {
    await router.navigate("/settings");
  });
  let dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/unsaved changes/i)).toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole("button", { name: /keep editing/i }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(router.state.location.pathname).toBe("/templates/pr-review");
  expect(screen.getByLabelText(/^name/i)).toHaveValue("PR review checklist!");
  // Browser Back.
  await act(async () => {
    await router.navigate(-1);
  });
  dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("button", { name: /discard and leave/i }));
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
});

test("a clean editor is not blocked, and a save is not blocked by its own dirty state (#907 review)", async () => {
  mocked.updateTemplate.mockResolvedValue(tpl({ name: "Renamed", updated_at: 1_788_440_000 }));
  const router = makeRouter(["/templates/pr-review"]);
  render(<RouterProvider router={router} />);
  await screen.findByLabelText(/^name/i);
  await act(async () => {
    await router.navigate("/settings");
  });
  expect(await screen.findByText("settings route")).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => {
    await router.navigate("/templates/pr-review");
  });
  const name = await screen.findByLabelText(/^name/i);
  await userEvent.clear(name);
  await userEvent.type(name, "Renamed");
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("Save and leaving are fenced while an upload is in flight; each finished upload is committed as it lands (#907 review)", async () => {
  let resolveFirst: (v: { path: string; name: string; stored: string }) => void = () => {};
  mocked.upload
    .mockImplementationOnce(() => new Promise((r) => { resolveFirst = r; }))
    .mockRejectedValueOnce(new ApiError(413, "file too large (max 25 MB)"));
  const router = makeRouter(["/templates", "/templates/pr-review"], 1);
  render(<RouterProvider router={router} />);
  const name = await screen.findByLabelText(/^name/i);
  await userEvent.clear(name);
  await userEvent.type(name, "Renamed");
  const save = screen.getByRole("button", { name: /^save$/i });
  expect(save).toBeEnabled();
  const a = new File([new Uint8Array([1])], "a.png", { type: "image/png" });
  const b = new File([new Uint8Array([2])], "b.png", { type: "image/png" });
  await userEvent.upload(screen.getByLabelText(/choose images/i), [a, b]);
  // First upload pending: Save is fenced, and so is leaving.
  await waitFor(() => expect(save).toBeDisabled());
  expect(save).toHaveAttribute("title", "Wait for the upload to finish");
  await act(async () => {
    await router.navigate(-1);
  });
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/upload in progress/i)).toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole("button", { name: /keep editing/i }));
  // The first lands → committed immediately; the second fails → the first is kept, the error
  // names the file and says what was kept.
  await act(async () => {
    resolveFirst({ path: "/home/u/.agent-sessions/uploads/20260903-3-a.png", name: "a.png", stored: "20260903-3-a.png" });
  });
  expect(await screen.findByRole("img", { name: "a.png" })).toBeInTheDocument();
  expect(await screen.findByRole("alert")).toHaveTextContent(/b\.png: file too large.*1 image before it added/i);
  await waitFor(() => expect(save).toBeEnabled());
  await userEvent.click(save);
  await waitFor(() => expect(mocked.updateTemplate).toHaveBeenCalledTimes(1));
  expect(mocked.updateTemplate.mock.calls[0][1].images).toEqual([
    { name: "shot.png", path: "/home/u/.agent-sessions/uploads/20260903-1-shot.png" },
    { name: "a.png", path: "/home/u/.agent-sessions/uploads/20260903-3-a.png" },
  ]);
});

// --- Round-2 findings on #907 -------------------------------------------------------------------

test("an older route load never lands in a newer editor: A→B with responses resolving B then A (#907 round 2)", async () => {
  const a = tpl({ id: "a", name: "Template A" });
  const b = tpl({ id: "b", name: "Template B" });
  const pending: ((v: unknown) => void)[] = [];
  mocked.templates.mockImplementation(() => new Promise((resolve) => pending.push(resolve)));
  mocked.updateTemplate.mockResolvedValue(b);
  const router = makeRouter(["/templates/a"]);
  render(<RouterProvider router={router} />);
  await waitFor(() => expect(pending).toHaveLength(1));
  await act(async () => {
    await router.navigate("/templates/b");
  });
  await waitFor(() => expect(pending).toHaveLength(2));
  // B's request resolves first, then A's (the stale one) — A must not overwrite B's editor.
  await act(async () => {
    pending[1]({ templates: [a, b], limits: LIMITS });
  });
  expect(await screen.findByLabelText(/^name/i)).toHaveValue("Template B");
  await act(async () => {
    pending[0]({ templates: [a, b], limits: LIMITS });
  });
  await new Promise((r) => setTimeout(r, 50));
  expect(screen.getByLabelText(/^name/i)).toHaveValue("Template B");
  // And a save from here targets B.
  await userEvent.type(screen.getByLabelText(/^name/i), "!");
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  await waitFor(() => expect(mocked.updateTemplate).toHaveBeenCalledTimes(1));
  expect(mocked.updateTemplate.mock.calls[0][0]).toBe("b");
});

test("the form is frozen while a save is out, and every mutation is fenced against every other (#907 round 2)", async () => {
  let resolveSave: (v: Template) => void = () => {};
  mocked.updateTemplate.mockImplementation(() => new Promise((r) => { resolveSave = r; }));
  renderEditor("/templates/pr-review");
  const name = await screen.findByLabelText(/^name/i);
  await userEvent.type(name, "!");
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  // Save is out: inputs are disabled (no edit can slip in), Add image / Duplicate / Delete too.
  await waitFor(() => expect(name).toBeDisabled());
  expect(screen.getByLabelText(/instructions/i)).toBeDisabled();
  expect(screen.getByRole("button", { name: /add image/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /duplicate/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /delete template/i })).toBeDisabled();
  await userEvent.type(name, "typed-during-save"); // disabled → ignored
  expect(name).toHaveValue("PR review checklist!");
  await act(async () => {
    resolveSave(tpl({ name: "PR review checklist!", updated_at: 1_788_440_000 }));
  });
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
});

test("an upload in flight fences Duplicate and Delete, not only Save (#907 round 2)", async () => {
  let resolveUpload: (v: { path: string; name: string; stored: string }) => void = () => {};
  mocked.upload.mockImplementation(() => new Promise((r) => { resolveUpload = r; }));
  renderEditor("/templates/pr-review");
  await screen.findByLabelText(/^name/i);
  const file = new File([new Uint8Array([1])], "a.png", { type: "image/png" });
  await userEvent.upload(screen.getByLabelText(/choose images/i), file);
  await waitFor(() => expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled());
  expect(screen.getByRole("button", { name: /duplicate/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /delete template/i })).toBeDisabled();
  await act(async () => {
    resolveUpload({ path: "/home/u/.agent-sessions/uploads/20260903-3-a.png", name: "a.png", stored: "20260903-3-a.png" });
  });
  await waitFor(() => expect(screen.getByRole("button", { name: /duplicate/i })).toBeEnabled());
  expect(screen.getByRole("button", { name: /delete template/i })).toBeEnabled();
});

test("Duplicate lands in a fresh editor whose leave guard is armed again (#907 round 2)", async () => {
  const copy = tpl({ id: "pr-review-2", name: "PR review checklist (copy)" });
  mocked.createTemplate.mockResolvedValue(copy);
  mocked.templates.mockResolvedValue({ templates: [tpl(), copy], limits: LIMITS });
  const router = makeRouter(["/templates/pr-review"]);
  render(<RouterProvider router={router} />);
  await screen.findByLabelText(/^name/i);
  await userEvent.click(screen.getByRole("button", { name: /duplicate/i }));
  await waitFor(() => expect(router.state.location.pathname).toBe("/templates/pr-review-2"));
  const name = await screen.findByDisplayValue("PR review checklist (copy)");
  await userEvent.type(name, "!");
  await act(async () => {
    await router.navigate("/settings");
  });
  // The reused-route bypass of the first cut let this through silently.
  expect(await screen.findByRole("dialog")).toHaveTextContent(/unsaved changes/i);
  expect(router.state.location.pathname).toBe("/templates/pr-review-2");
});

test("beforeunload is armed while an upload is in flight, even before the form commits it (#907 round 2)", async () => {
  const added = vi.spyOn(window, "addEventListener");
  let resolveUpload: (v: { path: string; name: string; stored: string }) => void = () => {};
  mocked.upload.mockImplementation(() => new Promise((r) => { resolveUpload = r; }));
  renderEditor("/templates/pr-review");
  await screen.findByLabelText(/^name/i);
  expect(added.mock.calls.filter((c) => c[0] === "beforeunload")).toHaveLength(0); // clean editor
  const file = new File([new Uint8Array([1])], "a.png", { type: "image/png" });
  await userEvent.upload(screen.getByLabelText(/choose images/i), file);
  await waitFor(() => expect(added.mock.calls.filter((c) => c[0] === "beforeunload")).toHaveLength(1));
  await act(async () => {
    resolveUpload({ path: "/home/u/.agent-sessions/uploads/20260903-3-a.png", name: "a.png", stored: "20260903-3-a.png" });
  });
  added.mockRestore();
});

// --- Round-3 findings on #907 -------------------------------------------------------------------

test("the preview substitutes before it trims, exactly as the paste does — a default with whitespace (#907 round 3)", async () => {
  const t = tpl({
    id: "ws",
    body: "{{value}}",
    fields: [{ name: "value", label: "Value", default: " x ", required: false }],
    images: [],
  });
  mocked.templates.mockResolvedValue({ templates: [t], limits: LIMITS });
  renderEditor("/templates/ws");
  const pre = await screen.findByLabelText(/what the agent receives/i);
  expect(pre.textContent).toBe("x");
  expect(pre.textContent).toBe(renderTemplate(t, defaultValues(t.fields)));
  // And with an image: one space, then the path — the assembly's join.
  const withImage = { ...t, images: [{ name: "a.png", path: "/u/.agent-sessions/uploads/20260903-1-a.png" }] };
  mocked.templates.mockResolvedValue({ templates: [withImage], limits: LIMITS });
  const { router } = renderEditor("/templates/ws");
  await act(async () => {
    await router.navigate("/templates/ws");
  });
  const pres = await screen.findAllByLabelText(/what the agent receives/i);
  expect(pres[pres.length - 1].textContent).toBe("x /u/.agent-sessions/uploads/20260903-1-a.png");
});

test("a pending Duplicate cannot navigate after the operator left; leaving mid-mutation asks first (#907 round 3)", async () => {
  let resolveCreate: (v: Template) => void = () => {};
  mocked.createTemplate.mockImplementation(() => new Promise((r) => { resolveCreate = r; }));
  const router = makeRouter(["/templates", "/templates/pr-review"], 1);
  render(<RouterProvider router={router} />);
  await screen.findByLabelText(/^name/i);
  await userEvent.click(screen.getByRole("button", { name: /duplicate/i }));
  // The mutation owns the route: Cancel asks rather than leaving silently.
  await userEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/save in progress/i)).toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole("button", { name: /discard and leave/i }));
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
  // The late response lands after the operator left: no hijack.
  await act(async () => {
    resolveCreate(tpl({ id: "late-copy", name: "late" }));
  });
  await new Promise((r) => setTimeout(r, 50));
  expect(router.state.location.pathname).toBe("/templates");
});

// --- Round-4 findings on #907 -------------------------------------------------------------------

test("discarding mid-batch ends the batch: a two-file pick, leave while A is pending, A lands — B is never started (#907 round 4)", async () => {
  let resolveA: (v: { path: string; name: string; stored: string }) => void = () => {};
  mocked.upload.mockImplementation(() => new Promise((r) => { resolveA = r; }));
  const router = makeRouter(["/templates", "/templates/pr-review"], 1);
  render(<RouterProvider router={router} />);
  await screen.findByLabelText(/^name/i);
  const a = new File([new Uint8Array([1])], "a.png", { type: "image/png" });
  const b = new File([new Uint8Array([2])], "b.png", { type: "image/png" });
  await userEvent.upload(screen.getByLabelText(/choose images/i), [a, b]);
  await waitFor(() => expect(mocked.upload).toHaveBeenCalledTimes(1));
  await userEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/upload in progress/i)).toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole("button", { name: /discard and leave/i }));
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
  // A lands after the exit: ownership was discarded, so B must not be stored on the operator's
  // behalf — the unfixed loop went straight on to `upload(B)`.
  await act(async () => {
    resolveA({ path: "/home/u/.agent-sessions/uploads/20260903-3-a.png", name: "a.png", stored: "20260903-3-a.png" });
  });
  await new Promise((r) => setTimeout(r, 50));
  expect(mocked.upload).toHaveBeenCalledTimes(1);
});

test("beforeunload is armed while a clean template's Duplicate is out on the wire (#907 round 4)", async () => {
  const added = vi.spyOn(window, "addEventListener");
  let resolveCreate: (v: Template) => void = () => {};
  mocked.createTemplate.mockImplementation(() => new Promise((r) => { resolveCreate = r; }));
  renderEditor("/templates/pr-review");
  await screen.findByLabelText(/^name/i);
  expect(added.mock.calls.filter((c) => c[0] === "beforeunload")).toHaveLength(0); // clean, idle
  await userEvent.click(screen.getByRole("button", { name: /duplicate/i }));
  // `dirty` is false the whole time — only `saving` can arm this.
  await waitFor(() => expect(added.mock.calls.filter((c) => c[0] === "beforeunload")).toHaveLength(1));
  await act(async () => {
    resolveCreate(tpl({ id: "pr-review-2", name: "PR review checklist (copy)" }));
  });
  added.mockRestore();
});

test("beforeunload is armed while a clean template's confirmed Delete is out on the wire (#907 round 4)", async () => {
  const added = vi.spyOn(window, "addEventListener");
  let resolveDelete: () => void = () => {};
  mocked.deleteTemplate.mockImplementation(() => new Promise<void>((r) => { resolveDelete = r; }));
  renderEditor("/templates/pr-review");
  await userEvent.click(await screen.findByRole("button", { name: /delete template/i }));
  const dialog = await screen.findByRole("dialog");
  expect(added.mock.calls.filter((c) => c[0] === "beforeunload")).toHaveLength(0); // asking is not mutating
  await userEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));
  await waitFor(() => expect(added.mock.calls.filter((c) => c[0] === "beforeunload")).toHaveLength(1));
  await act(async () => {
    resolveDelete();
  });
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
  added.mockRestore();
});

test("uncommitted tag text is unsaved work: beforeunload arms and Back asks while the tag input still has focus (#907 addendum)", async () => {
  const added = vi.spyOn(window, "addEventListener");
  const router = makeRouter(["/templates", "/templates/pr-review"], 1);
  render(<RouterProvider router={router} />);
  await screen.findByLabelText(/^name/i);
  expect(added.mock.calls.filter((c) => c[0] === "beforeunload")).toHaveLength(0);
  const tagInput = screen.getByLabelText(/add a tag/i);
  await userEvent.type(tagInput, "ops");
  expect(tagInput).toHaveFocus(); // nothing has committed the draft yet
  expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled(); // `dirty` itself is untouched
  await waitFor(() => expect(added.mock.calls.filter((c) => c[0] === "beforeunload")).toHaveLength(1));
  await act(async () => {
    await router.navigate(-1);
  });
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/unsaved changes/i)).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/templates/pr-review");
  added.mockRestore();
});

test("Save-as-template lands prefilled from router state: body + images, dirty, save enabled once named (#905 P3)", async () => {
  mocked.createTemplate.mockResolvedValue(tpl({ id: "kept", name: "Kept" }));
  const router = makeRouter([
    {
      pathname: "/templates/new",
      state: {
        prefill: {
          body: "keep me {{x}}",
          images: [{ name: "a.png", path: "/u/.agent-sessions/uploads/20260903-1-a.png" }],
        },
      },
    },
  ]);
  render(<RouterProvider router={router} />);
  expect(await screen.findByLabelText(/instructions/i)).toHaveValue("keep me {{x}}");
  expect(await screen.findByRole("img", { name: "a.png" })).toHaveAttribute("src", "blob:test/1");
  expect(mocked.uploadBlob).toHaveBeenCalledWith("/u/.agent-sessions/uploads/20260903-1-a.png", expect.anything());
  expect(screen.getByText(/prefilled from a sent message/i)).toBeInTheDocument();
  expect(screen.getByText(/\{\{x\}\} names no field/i)).toBeInTheDocument();
  const save = screen.getByRole("button", { name: /^save$/i });
  expect(save).toBeDisabled();
  await userEvent.type(screen.getByLabelText(/^name/i), "Kept");
  expect(save).toBeEnabled();
  await userEvent.click(save);
  await waitFor(() => expect(mocked.createTemplate).toHaveBeenCalledTimes(1));
  expect(mocked.createTemplate.mock.calls[0][0]).toMatchObject({
    name: "Kept",
    body: "keep me {{x}}",
    images: [{ name: "a.png", path: "/u/.agent-sessions/uploads/20260903-1-a.png" }],
  });
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
});

test("the save-as-template prefill is consumed once: the entry's state is cleared, and Back after the save is an empty editor (#908 review)", async () => {
  mocked.createTemplate.mockResolvedValue(tpl({ id: "kept", name: "Kept" }));
  const router = makeRouter([
    { pathname: "/templates/new", state: { prefill: { body: "keep me", images: [] } } },
  ]);
  render(<RouterProvider router={router} />);
  expect(await screen.findByLabelText(/instructions/i)).toHaveValue("keep me");
  // The history entry's state is gone the moment the form has taken it — a reload of this
  // route cannot resurrect the payload — while the form keeps what it took.
  await waitFor(() => expect(router.state.location.state).toBeNull());
  expect(router.state.location.pathname).toBe("/templates/new");
  expect(screen.getByLabelText(/instructions/i)).toHaveValue("keep me");
  await userEvent.type(screen.getByLabelText(/^name/i), "Kept");
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  expect(await screen.findByText("gallery route")).toBeInTheDocument();
  // Back returns to /templates/new as an EMPTY new template, not the saved payload again.
  await act(async () => {
    await router.navigate(-1);
  });
  expect(await screen.findByLabelText(/instructions/i)).toHaveValue("");
  expect(screen.queryByText(/prefilled from a sent message/i)).toBeNull();
});

// ---- a field's source (#1090) ------------------------------------------------------------------

test("switching a field to Library clears its default, shows the library's value, previews it, and saves source=library", async () => {
  mocked.templateVariables.mockResolvedValue({
    variables: [{ name: "issue_ref", value: "ACME-7", created_at: 1, updated_at: 1, used_by: [] }],
    limits: { variables_max: 100, value_max: 2000, name_max: 32 },
  });
  mocked.updateTemplate.mockResolvedValue(tpl({ updated_at: 1_788_440_000 }));
  renderEditor("/templates/pr-review");
  const source = await screen.findByLabelText(/field 2 source/i);
  expect(screen.getByLabelText(/field 2 default/i)).toHaveValue("the linked issue");
  await userEvent.selectOptions(source, "library");
  // The default input is gone: a library field owns no value of its own.
  expect(screen.queryByLabelText(/field 2 default/i)).not.toBeInTheDocument();
  expect(screen.getByText("ACME-7")).toBeInTheDocument();
  const pre = screen.getByLabelText(/what the agent receives/i);
  expect(pre.textContent).toContain("for ACME-7 and");
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  await waitFor(() => expect(mocked.updateTemplate).toHaveBeenCalledTimes(1));
  const input = mocked.updateTemplate.mock.calls[0][1];
  expect(input.fields[1]).toEqual({
    name: "issue_ref",
    label: "Issue",
    default: "",
    required: false,
    source: "library",
  });
});

test("a library field whose variable does not exist says so and previews its token", async () => {
  mocked.templateVariables.mockResolvedValue({
    variables: [],
    limits: { variables_max: 100, value_max: 2000, name_max: 32 },
  });
  renderEditor("/templates/pr-review");
  await userEvent.selectOptions(await screen.findByLabelText(/field 2 source/i), "library");
  expect(await screen.findByText(/missing library variable/i)).toBeInTheDocument();
  expect(screen.getByLabelText(/what the agent receives/i).textContent).toContain(
    "for {{issue_ref}} and",
  );
});

test("a field set to Secret drops its default, previews a mask, and saves kind=secret (#1090 Phase 2)", async () => {
  mocked.templateVariables.mockResolvedValue({
    variables: [],
    limits: { variables_max: 100, value_max: 2000, name_max: 32, secret_min: 8 },
  });
  mocked.updateTemplate.mockResolvedValue(tpl({ updated_at: 1_788_440_000 }));
  renderEditor("/templates/pr-review");
  const kind = await screen.findByLabelText(/field 2 kind/i);
  await userEvent.selectOptions(kind, "secret");
  expect(screen.queryByLabelText(/field 2 default/i)).not.toBeInTheDocument();
  expect(screen.getByText(/typed at send time · not stored/i)).toBeInTheDocument();
  expect(screen.getByLabelText(/what the agent receives/i).textContent).toContain(
    "for [secret: issue_ref] and",
  );
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  await waitFor(() => expect(mocked.updateTemplate).toHaveBeenCalledTimes(1));
  expect(mocked.updateTemplate.mock.calls[0][1].fields[1]).toMatchObject({
    name: "issue_ref",
    default: "",
    kind: "secret",
  });
});
