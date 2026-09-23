import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  createMemoryRouter,
  MemoryRouter,
  Route,
  Routes,
  RouterProvider,
  useLocation,
} from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError, setApiFetch } from "../lib/api";
import type { Template, TemplatesResponse, TemplateVariable } from "../types/api";
import Templates from "./Templates";
import { SessionsCtx } from "../app/sessionsStore";
import type { Session } from "../types/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      templates: vi.fn(),
      createTemplate: vi.fn(),
      deleteTemplate: vi.fn(),
      templateVariables: vi.fn(() =>
        Promise.resolve({ variables: [], limits: { variables_max: 100, value_max: 2000, name_max: 32 } }),
      ),
      createTemplateVariable: vi.fn(),
      updateTemplateVariable: vi.fn(),
      deleteTemplateVariable: vi.fn(),
      // Thumbnails go through the seam, never a native <img src> (Home Free tunnel, #907 review).
      uploadBlob: vi.fn(() => Promise.resolve(new Blob([new Uint8Array([137, 80, 78, 71])]))),
    },
  };
});

// jsdom has no object URLs; the gallery only needs them to be distinct and revocable.
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
  image_suffixes: [".png"],
};

function tpl(over: Partial<Template> = {}): Template {
  return {
    id: "pr-review",
    name: "PR review checklist",
    description: "Review a PR against the checklist.",
    tags: ["review", "forgejo"],
    body: "Review PR {{pr_url}}",
    fields: [{ name: "pr_url", label: "PR link", default: "", required: true }],
    images: [{ name: "shot.png", path: "/home/u/.agent-sessions/uploads/20260903-1-shot.png" }],
    created_at: 1_788_400_000,
    updated_at: 1_788_430_000.5,
    used_count: 14,
    last_used_at: 1_788_432_000,
    ...over,
  };
}

function renderGallery() {
  return render(
    <MemoryRouter initialEntries={["/templates"]}>
      <Routes>
        <Route path="/templates" element={<Templates />} />
        <Route path="/templates/:id" element={<p>editor route</p>} />
        <Route path="/templates/new" element={<p>new route</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

const mocked = api as unknown as {
  templates: ReturnType<typeof vi.fn>;
  createTemplate: ReturnType<typeof vi.fn>;
  deleteTemplate: ReturnType<typeof vi.fn>;
};

beforeEach(() => {
  vi.clearAllMocks();
});

test("lists the library with thumbnails keyed by the STORED basename, tags and the meta line", async () => {
  mocked.templates.mockResolvedValue({
    templates: [tpl(), tpl({ id: "deploy", name: "Deploy watch", tags: ["ops"], images: [], fields: [], used_count: 0, last_used_at: null })],
    limits: LIMITS,
  } satisfies TemplatesResponse);
  const { unmount } = renderGallery();
  const list = await screen.findByRole("list", { name: /templates/i });
  const cards = within(list).getAllByRole("listitem");
  expect(cards).toHaveLength(2);
  // The thumbnail's bytes come through the seam (`uploadBlob` with the stored path), and the
  // <img> carries an object URL — never a native `/api/uploads/...` src.
  const img = await within(cards[0]).findByRole("presentation", { hidden: true });
  expect(img).toHaveAttribute("src", "blob:test/1");
  expect(api.uploadBlob).toHaveBeenCalledWith("/home/u/.agent-sessions/uploads/20260903-1-shot.png", expect.anything());
  expect(within(cards[0]).getByText(/1 field · 1 image · used 14×/i)).toBeInTheDocument();
  expect(within(cards[1]).getByText(/never used/i)).toBeInTheDocument();
  expect(within(cards[1]).queryByRole("presentation", { hidden: true })).toBeNull();
  // Chips count over the unfiltered set.
  const group = screen.getByRole("group", { name: /filter by tag/i });
  expect(within(group).getByRole("button", { name: /^all 2$/i })).toHaveAttribute("aria-pressed", "true");
  expect(within(group).getByRole("button", { name: /^review 1$/i })).toBeInTheDocument();
  // The object URL is revoked on unmount.
  unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test/1");
});

test("a tag chip and the search each narrow the grid; the chips keep their counts", async () => {
  mocked.templates.mockResolvedValue({
    templates: [tpl(), tpl({ id: "deploy", name: "Deploy watch", tags: ["ops"], images: [] })],
    limits: LIMITS,
  });
  renderGallery();
  await screen.findByRole("list", { name: /templates/i });
  await userEvent.click(screen.getByRole("button", { name: /^ops 1$/i }));
  expect(screen.getAllByRole("listitem")).toHaveLength(1);
  expect(screen.getByText("Deploy watch")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^review 1$/i })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^ops 1$/i })); // toggles off
  // Both fixtures share a body, so search on the one thing that differs: the name.
  await userEvent.type(screen.getByRole("searchbox", { name: /search templates/i }), "pr review");
  expect(screen.getAllByRole("listitem")).toHaveLength(1);
  expect(screen.getByText("PR review checklist")).toBeInTheDocument();
  await userEvent.clear(screen.getByRole("searchbox", { name: /search templates/i }));
  await userEvent.type(screen.getByRole("searchbox", { name: /search templates/i }), "zzz");
  expect(screen.getByText(/no template matches/i)).toBeInTheDocument();
});

test("an empty library is the empty state, not a blank grid", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  renderGallery();
  expect(await screen.findByText(/no templates yet/i)).toBeInTheDocument();
  expect(screen.queryByRole("list", { name: /templates/i })).toBeNull();
  expect(screen.queryByRole("searchbox")).toBeNull();
});

test("delete asks first, sends the card's updated_at as the fence, then reloads", async () => {
  mocked.templates
    .mockResolvedValueOnce({ templates: [tpl()], limits: LIMITS })
    .mockResolvedValueOnce({ templates: [], limits: LIMITS });
  mocked.deleteTemplate.mockResolvedValue(undefined);
  renderGallery();
  await userEvent.click(await screen.findByRole("button", { name: /delete pr review checklist/i }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByText(/image stays in the uploads folder/i)).toBeInTheDocument();
  expect(mocked.deleteTemplate).not.toHaveBeenCalled();
  await userEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));
  await waitFor(() => expect(mocked.deleteTemplate).toHaveBeenCalledWith("pr-review", 1_788_430_000.5));
  expect(await screen.findByText(/no templates yet/i)).toBeInTheDocument();
  expect(screen.getByText(/deleted “pr review checklist”/i)).toBeInTheDocument();
});

test("a 409 on delete deletes nothing, says so, and reloads the current record", async () => {
  mocked.templates
    .mockResolvedValueOnce({ templates: [tpl()], limits: LIMITS })
    .mockResolvedValueOnce({ templates: [tpl({ name: "Renamed elsewhere" })], limits: LIMITS });
  mocked.deleteTemplate.mockRejectedValue(
    new ApiError(409, "template changed since you loaded it", { current: tpl({ name: "Renamed elsewhere" }) }),
  );
  renderGallery();
  await userEvent.click(await screen.findByRole("button", { name: /delete pr review checklist/i }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /^delete$/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/changed elsewhere.*nothing deleted/i);
  expect(await screen.findByText("Renamed elsewhere")).toBeInTheDocument();
  expect(mocked.templates).toHaveBeenCalledTimes(2);
});

test("duplicate creates a copy with the same content and opens it", async () => {
  mocked.templates.mockResolvedValue({ templates: [tpl()], limits: LIMITS });
  mocked.createTemplate.mockResolvedValue(tpl({ id: "pr-review-2", name: "PR review checklist (copy)" }));
  renderGallery();
  await userEvent.click(await screen.findByRole("button", { name: /duplicate pr review checklist/i }));
  await waitFor(() => expect(mocked.createTemplate).toHaveBeenCalledTimes(1));
  const sent = mocked.createTemplate.mock.calls[0][0];
  expect(sent).toEqual({
    name: "PR review checklist (copy)",
    description: tpl().description,
    tags: tpl().tags,
    body: tpl().body,
    fields: tpl().fields,
    images: tpl().images,
  });
  expect(await screen.findByText("editor route")).toBeInTheDocument();
});

test("app mode: thumbnail bytes ride the injected fetch seam; no request escapes to the native origin (#907 review)", async () => {
  // The real `uploadBlob` (not the mock) against an injected tunnel fetch — the Home Free shape.
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  mocked.templates.mockResolvedValue({ templates: [tpl()], limits: LIMITS });
  // Bytes, not a jsdom Blob: Node's Response wants a Blob with `.stream()`, which jsdom's lacks.
  const tunnel = vi.fn(async () => new Response(new Uint8Array([1, 2, 3]), { status: 200 }));
  const native = vi.spyOn(globalThis, "fetch");
  actual.setApiFetch(tunnel as unknown as Parameters<typeof setApiFetch>[0]);
  (api as unknown as { uploadBlob: unknown }).uploadBlob = actual.api.uploadBlob;
  try {
    renderGallery();
    await screen.findByRole("list", { name: /templates/i });
    await waitFor(() => expect(tunnel).toHaveBeenCalledWith("/api/uploads/20260903-1-shot.png", expect.anything()));
    const img = await screen.findByRole("presentation", { hidden: true });
    expect(img).toHaveAttribute("src", "blob:test/1");
    expect(native.mock.calls.filter((c) => String(c[0]).includes("/api/uploads"))).toHaveLength(0);
  } finally {
    actual.setApiFetch(undefined);
    native.mockRestore();
  }
});

test("a pending gallery Duplicate cannot navigate after the operator moved on (#907 round 3)", async () => {
  mocked.templates.mockResolvedValue({ templates: [tpl()], limits: LIMITS });
  let resolveCreate: (v: Template) => void = () => {};
  mocked.createTemplate.mockImplementation(() => new Promise((r) => { resolveCreate = r; }));
  const router = createMemoryRouter(
    [
      { path: "/templates", element: <Templates /> },
      { path: "/templates/new", element: <p>new route</p> },
      { path: "/templates/:id", element: <p>editor route</p> },
    ],
    { initialEntries: ["/templates"] },
  );
  render(<RouterProvider router={router} />);
  await userEvent.click(await screen.findByRole("button", { name: /duplicate pr review checklist/i }));
  await act(async () => {
    await router.navigate("/templates/new");
  });
  expect(await screen.findByText("new route")).toBeInTheDocument();
  await act(async () => {
    resolveCreate(tpl({ id: "late-copy" }));
  });
  await new Promise((r) => setTimeout(r, 50));
  expect(router.state.location.pathname).toBe("/templates/new");
});

test("a deleted card never stays actionable when the refresh fails, and the message says so (#907 round 3)", async () => {
  mocked.templates
    .mockResolvedValueOnce({ templates: [tpl(), tpl({ id: "keep", name: "Keep me", images: [] })], limits: LIMITS })
    .mockRejectedValueOnce(new ApiError(502, "gateway down"));
  mocked.deleteTemplate.mockResolvedValue(undefined);
  renderGallery();
  await userEvent.click(await screen.findByRole("button", { name: /delete pr review checklist/i }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /^delete$/i }));
  await waitFor(() => expect(mocked.deleteTemplate).toHaveBeenCalledTimes(1));
  // The row is gone locally even though the reload failed…
  await waitFor(() => expect(screen.queryByText("PR review checklist")).toBeNull());
  expect(screen.getByText("Keep me")).toBeInTheDocument();
  // …and nothing claims the gallery was reloaded.
  expect(await screen.findByText(/could not be refreshed/i)).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent(/gateway down/i);
});

test("a 409 whose reload fails does not claim to have reloaded (#907 round 3)", async () => {
  mocked.templates
    .mockResolvedValueOnce({ templates: [tpl()], limits: LIMITS })
    .mockRejectedValueOnce(new ApiError(502, "gateway down"));
  mocked.deleteTemplate.mockRejectedValue(new ApiError(409, "changed", { current: tpl() }));
  renderGallery();
  await userEvent.click(await screen.findByRole("button", { name: /delete pr review checklist/i }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /^delete$/i }));
  const alert = await screen.findByRole("alert");
  await waitFor(() => expect(alert).toHaveTextContent(/reloading the gallery failed/i));
  expect(alert).not.toHaveTextContent(/— reloaded/i);
  expect(screen.getByText("PR review checklist")).toBeInTheDocument(); // nothing deleted
});

test("a 404 on delete drops the card locally even when the refresh fails — the server already said it is gone (#907 round 4)", async () => {
  mocked.templates
    .mockResolvedValueOnce({ templates: [tpl(), tpl({ id: "keep", name: "Keep me", images: [] })], limits: LIMITS })
    .mockRejectedValueOnce(new ApiError(502, "gateway down"));
  mocked.deleteTemplate.mockRejectedValue(new ApiError(404, "no such template"));
  renderGallery();
  await userEvent.click(await screen.findByRole("button", { name: /delete pr review checklist/i }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /^delete$/i }));
  // The unfixed branch kept the server-absent card in place when the follow-up GET failed.
  await waitFor(() => expect(screen.queryByText("PR review checklist")).toBeNull());
  expect(screen.getByText("Keep me")).toBeInTheDocument();
  const alert = await screen.findByRole("alert");
  await waitFor(() => expect(alert).toHaveTextContent(/already deleted elsewhere.*reloading the gallery failed/i));
  expect(alert).not.toHaveTextContent(/— reloaded/i);
});

test("a failed first load is not 'loading' forever: the header says not loaded, the alert offers Retry, and Retry loads (#907 addendum)", async () => {
  mocked.templates
    .mockRejectedValueOnce(new ApiError(502, "gateway down"))
    .mockResolvedValueOnce({ templates: [tpl()], limits: LIMITS });
  renderGallery();
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/gateway down/i);
  expect(screen.queryByText("loading")).toBeNull();
  expect(screen.getByText("not loaded")).toBeInTheDocument();
  await userEvent.click(within(alert).getByRole("button", { name: /^retry$/i }));
  expect(await screen.findByText("PR review checklist")).toBeInTheDocument();
  expect(screen.getByText("1 saved")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).toBeNull();
});

function ShowState() {
  const loc = useLocation();
  return <p>session route {JSON.stringify(loc.state)}</p>;
}

test("USE picks a session from the sidebar's store and lands there with the template staged (#905 P3)", async () => {
  mocked.templates.mockResolvedValue({ templates: [tpl()], limits: LIMITS });
  const rows = [
    { id: "claude:abc", engine: "claude", uuid: "abc", short_uuid: "abc", cwd: "/home/u/proj", title: "Older one", working: false, last_mtime: 10, archived: false },
    { id: "codex:def", engine: "codex", uuid: "def", short_uuid: "def", cwd: "/home/u/other", title: "Working one", working: true, last_mtime: 5, archived: false },
  ] as unknown as Session[];
  const store = { sessions: rows, setSessions: vi.fn(), looked: new Map(), lookup: vi.fn(), remember: vi.fn(), forget: vi.fn(), retryGen: 0 };
  render(
    <SessionsCtx.Provider value={store as never}>
      <MemoryRouter initialEntries={["/templates"]}>
        <Routes>
          <Route path="/templates" element={<Templates />} />
          <Route path="/s/:engine/:id" element={<ShowState />} />
        </Routes>
      </MemoryRouter>
    </SessionsCtx.Provider>,
  );
  await userEvent.click(await screen.findByRole("button", { name: /use pr review checklist/i }));
  const dialog = screen.getByRole("dialog");
  const options = within(dialog).getAllByRole("button", { name: /^use in /i });
  // Working sessions first, then most recent.
  expect(options.map((o) => o.getAttribute("aria-label"))).toEqual([
    "Use in Working one",
    "Use in Older one",
  ]);
  await userEvent.click(options[0]);
  expect(await screen.findByText(/session route/i)).toHaveTextContent('{"template":"pr-review"}');
});

// ---- VARIABLES (#1090) ---------------------------------------------------------------------------

const VAR_LIMITS = { variables_max: 100, value_max: 2000, name_max: 32 };

function variable(over: Partial<TemplateVariable> = {}): TemplateVariable {
  return {
    name: "staging_host",
    value: "staging.acme.test",
    created_at: 1,
    updated_at: 10,
    used_by: [],
    ...over,
  };
}

const vmocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function renderVariables() {
  return render(
    <MemoryRouter initialEntries={["/templates?tab=variables"]}>
      <Routes>
        <Route path="/templates" element={<Templates />} />
        <Route path="/templates/:id" element={<p>editor route</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

test("?tab=variables opens the library; both tab counts are real", async () => {
  mocked.templates.mockResolvedValue({ templates: [tpl()], limits: LIMITS });
  vmocked.templateVariables.mockResolvedValue({
    variables: [
      variable({ used_by: [{ id: "pr-review", name: "PR review checklist" }] }),
      variable({ name: "test_cmd", value: "uv run pytest -q" }),
    ],
    limits: VAR_LIMITS,
  });
  renderVariables();
  const list = await screen.findByRole("list", { name: /^variables$/i });
  const rows = within(list).getAllByRole("listitem").filter((li) => li.dataset.variable);
  expect(rows.map((r) => r.dataset.variable)).toEqual(["staging_host", "test_cmd"]);
  expect(within(rows[0]).getByText("{{staging_host}}")).toBeInTheDocument();
  expect(within(rows[0]).getByText("1 template")).toBeInTheDocument();
  expect(within(rows[1]).getByText(/not used yet/i)).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: /variables 2/i })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tab", { name: /templates 1/i })).toHaveAttribute("aria-selected", "false");
  // The gallery's own list is not rendered on this tab.
  expect(screen.queryByRole("list", { name: /^templates$/i })).not.toBeInTheDocument();
});

test("an empty library is an honest empty state, and NEW VARIABLE adds one", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateVariables.mockResolvedValue({ variables: [], limits: VAR_LIMITS });
  vmocked.createTemplateVariable.mockResolvedValue(variable({ name: "host", value: "a.test" }));
  renderVariables();
  expect(await screen.findByText(/no variables yet/i)).toBeInTheDocument();
  await userEvent.click(screen.getAllByRole("button", { name: /new variable/i })[0]);
  const form = screen.getByRole("form", { name: /new variable/i });
  const add = within(form).getByRole("button", { name: /^add$/i });
  expect(add).toBeDisabled();
  await userEvent.type(within(form).getByPlaceholderText("staging_host"), "9lives");
  // A name must start with a letter — refused before it can be sent.
  expect(within(form).getByText(/a-z, 0-9, _/i)).toBeInTheDocument();
  await userEvent.clear(within(form).getByPlaceholderText("staging_host"));
  await userEvent.type(within(form).getByPlaceholderText("staging_host"), "host");
  await userEvent.type(within(form).getByPlaceholderText("staging.acme.test"), "a.test");
  vmocked.templateVariables.mockResolvedValue({
    variables: [variable({ name: "host", value: "a.test" })],
    limits: VAR_LIMITS,
  });
  await userEvent.click(add);
  expect(vmocked.createTemplateVariable).toHaveBeenCalledWith({ name: "host", value: "a.test" });
  expect(await screen.findByText(/added \{\{host\}\}/i)).toBeInTheDocument();
});

test("an edit is fenced by updated_at, and a 409 reloads instead of overwriting", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateVariables.mockResolvedValue({ variables: [variable()], limits: VAR_LIMITS });
  vmocked.updateTemplateVariable.mockRejectedValueOnce(
    new ApiError(409, "variable changed since you loaded it", { current: variable({ value: "x" }) }),
  );
  renderVariables();
  await userEvent.click(await screen.findByRole("button", { name: /^edit staging_host$/i }));
  const input = screen.getByLabelText(/value of staging_host/i);
  await userEvent.clear(input);
  await userEvent.type(input, "new.acme.test");
  await userEvent.keyboard("{Control>}{Enter}{/Control}");
  expect(vmocked.updateTemplateVariable).toHaveBeenCalledWith("staging_host", "new.acme.test", 10);
  expect(await screen.findByRole("alert")).toHaveTextContent(/changed elsewhere — reloaded, nothing saved/i);
  expect(vmocked.templateVariables).toHaveBeenCalledTimes(2);
});

test("a delete refused because templates still use the variable names them, with links", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  const deps = [
    { id: "run-migration", name: "Run staging migration" },
    { id: "smoke", name: "Smoke test staging" },
  ];
  vmocked.templateVariables.mockResolvedValue({
    variables: [variable({ used_by: deps })],
    limits: VAR_LIMITS,
  });
  vmocked.deleteTemplateVariable.mockRejectedValue(
    new ApiError(409, "staging_host is still used by 2 templates", { dependants: deps }),
  );
  renderVariables();
  await userEvent.click(await screen.findByRole("button", { name: /^delete staging_host$/i }));
  const dialog = screen.getByRole("dialog");
  expect(dialog).toHaveTextContent(/2 templates use this variable/i);
  await userEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));
  expect(vmocked.deleteTemplateVariable).toHaveBeenCalledWith("staging_host", 10);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/can't delete \{\{staging_host\}\} — 2 templates still use it/i);
  expect(within(alert).getByRole("link", { name: "Smoke test staging" })).toHaveAttribute(
    "href",
    "/templates/smoke",
  );
});

test("a variables library that fails to load leaves the templates tab working", async () => {
  mocked.templates.mockResolvedValue({ templates: [tpl()], limits: LIMITS });
  vmocked.templateVariables.mockRejectedValue(new ApiError(500, "the template store failed"));
  renderGallery();
  expect(await screen.findByRole("list", { name: /^templates$/i })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: /variables –/i })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("tab", { name: /variables/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/the template store failed/i);
});

test("an edit is fenced by the revision it STARTED from, even after a refresh moved the row (#1095 review)", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateVariables.mockResolvedValue({ variables: [variable()], limits: VAR_LIMITS });
  vmocked.createTemplateVariable.mockResolvedValue(variable({ name: "other", value: "x" }));
  vmocked.updateTemplateVariable.mockResolvedValue(variable({ value: "mine", updated_at: 30 }));
  renderVariables();
  // Start editing at revision 10…
  await userEvent.click(await screen.findByRole("button", { name: /^edit staging_host$/i }));
  const input = screen.getByLabelText(/value of staging_host/i);
  await userEvent.clear(input);
  await userEvent.type(input, "mine");
  // …another tab saves it (revision 20), and an unrelated create here refreshes the list.
  vmocked.templateVariables.mockResolvedValue({
    variables: [variable({ value: "theirs", updated_at: 20 }), variable({ name: "other", value: "x" })],
    limits: VAR_LIMITS,
  });
  await userEvent.click(screen.getAllByRole("button", { name: /new variable/i })[0]);
  const form = screen.getByRole("form", { name: /new variable/i });
  await userEvent.type(within(form).getByPlaceholderText("staging_host"), "other");
  await userEvent.type(within(form).getByPlaceholderText("staging.acme.test"), "x");
  await userEvent.click(within(form).getByRole("button", { name: /^add$/i }));
  await screen.findByText(/added \{\{other\}\}/i);
  // The draft is still open, and its save carries 10 — so the server answers 409, not overwrite.
  await userEvent.click(screen.getByRole("button", { name: /^save staging_host$/i }));
  expect(vmocked.updateTemplateVariable).toHaveBeenCalledWith("staging_host", "mine", 10);
});

test("a save in flight freezes the draft and a second submit is ignored (#1095 review)", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateVariables.mockResolvedValue({ variables: [variable()], limits: VAR_LIMITS });
  let settle: (v: TemplateVariable) => void = () => {};
  vmocked.updateTemplateVariable.mockImplementation(
    () => new Promise<TemplateVariable>((r) => (settle = r)),
  );
  renderVariables();
  await userEvent.click(await screen.findByRole("button", { name: /^edit staging_host$/i }));
  const input = screen.getByLabelText(/value of staging_host/i);
  await userEvent.clear(input);
  await userEvent.type(input, "first");
  await userEvent.keyboard("{Control>}{Enter}{/Control}");
  // Pending: typing more does nothing, and a second Ctrl+Enter does not submit again.
  expect(input).toHaveAttribute("readonly");
  await userEvent.type(input, "second");
  expect(input).toHaveValue("first");
  await userEvent.keyboard("{Control>}{Enter}{/Control}");
  expect(vmocked.updateTemplateVariable).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: /^save staging_host$/i })).toBeDisabled();
  await act(async () => settle(variable({ value: "first", updated_at: 11 })));
});

test("a multi-line value keeps its lines when created and when edited (#1095 review)", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateVariables.mockResolvedValue({ variables: [], limits: VAR_LIMITS });
  vmocked.createTemplateVariable.mockResolvedValue(variable({ name: "run", value: "cd repo\nnpm test" }));
  renderVariables();
  await userEvent.click((await screen.findAllByRole("button", { name: /new variable/i }))[0]);
  const form = screen.getByRole("form", { name: /new variable/i });
  await userEvent.type(within(form).getByPlaceholderText("staging_host"), "run");
  // Enter is a newline in the value, never a submit.
  await userEvent.type(within(form).getByPlaceholderText("staging.acme.test"), "cd repo{Enter}npm test");
  expect(vmocked.createTemplateVariable).not.toHaveBeenCalled();
  await userEvent.click(within(form).getByRole("button", { name: /^add$/i }));
  expect(vmocked.createTemplateVariable).toHaveBeenCalledWith({ name: "run", value: "cd repo\nnpm test" });

  vmocked.templateVariables.mockResolvedValue({
    variables: [variable({ name: "run", value: "cd repo\nnpm test" })],
    limits: VAR_LIMITS,
  });
  vmocked.updateTemplateVariable.mockResolvedValue(variable({ name: "run", value: "x" }));
  cleanupAndRender();
  await userEvent.click(await screen.findByRole("button", { name: /^edit run$/i }));
  const input = screen.getByLabelText(/value of run/i);
  expect(input).toHaveValue("cd repo\nnpm test");
  await userEvent.type(input, "{Enter}npm run lint");
  await userEvent.click(screen.getByRole("button", { name: /^save run$/i }));
  expect(vmocked.updateTemplateVariable).toHaveBeenCalledWith(
    "run",
    "cd repo\nnpm test\nnpm run lint",
    10,
  );
});

function cleanupAndRender() {
  cleanup();
  renderVariables();
}

test("a 409 that is not an edit conflict shows the server's reason, never 'changed elsewhere' (#1095 review)", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateVariables.mockResolvedValue({ variables: [variable()], limits: VAR_LIMITS });
  const reason =
    "the template library could not be read in full — nothing was deleted, because a template might still use it";
  vmocked.deleteTemplateVariable.mockRejectedValue(new ApiError(409, reason, { detail: reason }));
  renderVariables();
  await userEvent.click(await screen.findByRole("button", { name: /^delete staging_host$/i }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /^delete$/i }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(reason);
  expect(alert).not.toHaveTextContent(/changed elsewhere/i);
});
