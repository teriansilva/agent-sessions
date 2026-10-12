import { act, render, screen, waitFor, within } from "@testing-library/react";
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
import type { Template, TemplatesResponse } from "../types/api";
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
      templateSuggestions: vi.fn(() => Promise.resolve({ result: null, configured: true })),
      suggestTemplates: vi.fn(),
      dismissSuggestion: vi.fn(() => Promise.resolve()),
      writeTemplate: vi.fn(),
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

// The old variables destination remains a replace-redirect. Shared-library behavior is covered
// by Variables.test.tsx and the real Python-backed appmode-shared-variables browser suite.
const vmocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
test("old variables tab links open the shared Library destination", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  render(<MemoryRouter initialEntries={["/templates?tab=variables"]}><Routes>
    <Route path="/templates" element={<Templates />} />
    <Route path="/library/variables" element={<p>Shared variable library</p>} />
  </Routes></MemoryRouter>);
  expect(await screen.findByText("Shared variable library")).toBeInTheDocument();
});

// ---- SUGGESTED (#1090 Phase 3) -------------------------------------------------------------------

const SUGGESTION_RESULT = {
  generated_at: 1,
  stats: { messages: 212, distinct: 90, sessions: 41, days: 30 },
  dropped: 1,
  suggestions: [
    {
      id: "aaaaaaaaaaaaaaaa",
      kind: "template" as const,
      name: "Fix review notes",
      reason: "You sent a variant of this 17 times.",
      count: 17,
      body: "Read the latest review on {{pr}}, fix every note, push.",
      fields: [{ name: "pr", label: "PR", default: "" }],
    },
    {
      id: "bbbbbbbbbbbbbbbb",
      kind: "variable" as const,
      name: "staging_host",
      reason: "Appears in 23 messages.",
      count: 23,
      value: "staging.acme.test",
      secret: false,
    },
    {
      id: "cccccccccccccccc",
      kind: "variable" as const,
      name: "deploy_token",
      reason: "Looks like a credential you paste.",
      count: 4,
      value: "",
      secret: true,
    },
  ],
};

function renderSuggested() {
  return render(
    <MemoryRouter initialEntries={["/templates?tab=suggested"]}>
      <Routes>
        <Route path="/templates" element={<Templates />} />
        <Route path="/templates/new" element={<ShowEditorState />} />
        <Route path="/library/variables" element={<ShowEditorState />} />
        <Route path="/settings/*" element={<p>settings route</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

function ShowEditorState() {
  const loc = useLocation();
  return <pre>editor state {JSON.stringify(loc.state)}</pre>;
}

test("SUGGESTED starts honest: nothing is analysed until ANALYSE, which then shows the drafts", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.suggestTemplates.mockResolvedValue(SUGGESTION_RESULT);
  renderSuggested();
  expect(await screen.findByText(/what template should you write/i)).toBeInTheDocument();
  expect(vmocked.suggestTemplates).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: /analyse my messages/i }));
  const list = await screen.findByRole("list", { name: /^suggestions$/i });
  expect(within(list).getAllByRole("listitem")).toHaveLength(3);
  expect(screen.getByText(/from 212 messages · 41 sessions · last 30 days/i)).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: /suggested 3/i })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByText(/looks like a credential — its value is not kept/i)).toBeInTheDocument();
  expect(screen.getByText(/1 proposal was left out/i)).toBeInTheDocument();
});

test("OPEN IN EDITOR hands the draft to a NEW template as its prefill — nothing is saved", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateSuggestions.mockResolvedValue({ result: SUGGESTION_RESULT, configured: true });
  renderSuggested();
  await userEvent.click(await screen.findByRole("button", { name: /open fix review notes in the editor/i }));
  const state = await screen.findByText(/editor state/);
  expect(state.textContent).toContain('"name":"Fix review notes"');
  expect(state.textContent).toContain('"body":"Read the latest review on {{pr}}, fix every note, push."');
  expect(state.textContent).toContain('"source":"template"');
  expect(mocked.createTemplate).not.toHaveBeenCalled();
});

test("ADD TO LIBRARY opens the new-variable form prefilled; a secret opens NEW SECRET with no value", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateSuggestions.mockResolvedValue({ result: SUGGESTION_RESULT, configured: true });
  const { unmount } = renderSuggested();
  await userEvent.click(await screen.findByRole("button", { name: /add staging_host to the library/i }));
  const state = await screen.findByText(/editor state/);
  expect(state.textContent).toContain('"variablePrefill":{"name":"staging_host","value":"staging.acme.test","secret":false}');
  expect(vmocked.createTemplateVariable).not.toHaveBeenCalled();
  unmount();
  renderSuggested();
  await userEvent.click(await screen.findByRole("button", { name: /add deploy_token to the library/i }));
  expect((await screen.findByText(/editor state/)).textContent).toContain('"variablePrefill":{"name":"deploy_token","value":"","secret":true}');
});

test("DISMISS removes a card for good; a failed dismiss puts it back and says so", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateSuggestions.mockResolvedValue({ result: SUGGESTION_RESULT, configured: true });
  renderSuggested();
  await userEvent.click(await screen.findByRole("button", { name: /dismiss staging_host/i }));
  expect(vmocked.dismissSuggestion).toHaveBeenCalledWith("bbbbbbbbbbbbbbbb");
  expect(screen.queryByText("{{staging_host}}")).toBeNull();
  expect(screen.getByRole("tab", { name: /suggested 2/i })).toBeInTheDocument();
  vmocked.dismissSuggestion.mockRejectedValueOnce(new ApiError(500, "store failed"));
  await userEvent.click(screen.getByRole("button", { name: /dismiss fix review notes/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/store failed/i);
  expect(screen.getByText("Fix review notes")).toBeInTheDocument();
});

test("a dismiss that fails after a newer analysis never puts its card into that result (#1110 review)", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateSuggestions.mockResolvedValue({ result: SUGGESTION_RESULT, configured: true });
  let fail!: (e: unknown) => void;
  vmocked.dismissSuggestion.mockImplementationOnce(
    () => new Promise<void>((_resolve, reject) => (fail = reject)),
  );
  vmocked.suggestTemplates.mockResolvedValueOnce(SUGGESTION_RESULT);
  renderSuggested();
  await userEvent.click(await screen.findByRole("button", { name: /dismiss staging_host/i }));
  expect(screen.queryByText("{{staging_host}}")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: /analyse again/i }));
  expect(await screen.findByRole("tab", { name: /suggested 3/i })).toBeInTheDocument();
  fail(new ApiError(500, "store failed"));
  expect(await screen.findByRole("alert")).toHaveTextContent(/store failed/i);
  expect(screen.getAllByText("{{staging_host}}")).toHaveLength(1);
  expect(screen.getByRole("tab", { name: /suggested 3/i })).toBeInTheDocument();
});

test("WRITE ME A TEMPLATE FOR… needs no analysis: the request's draft opens in the editor, library slots intact", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.writeTemplate.mockResolvedValue({
    template: {
      name: "Migrate staging",
      description: "Run the migrations on staging.",
      body: "ssh {{staging_host}} and migrate {{app}}",
      fields: [
        { name: "staging_host", label: "Host", default: "", source: "library", kind: "text" },
        { name: "app", label: "App", default: "api", source: "template", kind: "text" },
      ],
    },
  });
  renderSuggested();
  const box = await screen.findByLabelText(/write me a template for/i);
  expect(vmocked.suggestTemplates).not.toHaveBeenCalled();
  await userEvent.type(box, "run the migrations on staging");
  await userEvent.click(screen.getByRole("button", { name: /write it/i }));
  expect(vmocked.writeTemplate).toHaveBeenCalledWith("run the migrations on staging", expect.anything());
  const state = await screen.findByText(/editor state/);
  expect(state.textContent).toContain('"name":"Migrate staging"');
  expect(state.textContent).toContain('"source":"library"');
  expect(vmocked.suggestTemplates).not.toHaveBeenCalled();
  expect(mocked.createTemplate).not.toHaveBeenCalled();
});

test("a refused request says why and stays on the tab", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.writeTemplate.mockRejectedValueOnce(
    new ApiError(422, "Your request looks like it contains a password or token. Describe it with a {{placeholder}} instead — nothing was sent."),
  );
  renderSuggested();
  await userEvent.type(await screen.findByLabelText(/write me a template for/i), "use password=x");
  await userEvent.click(screen.getByRole("button", { name: /write it/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/nothing was sent/i);
  expect(screen.queryByText(/editor state/)).toBeNull();
});

test("no AI endpoint: says so and links to Settings; nothing can be analysed", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateSuggestions.mockResolvedValue({ result: null, configured: false });
  renderSuggested();
  expect(await screen.findByText(/no ai endpoint set up/i)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /open settings/i })).toHaveAttribute(
    "href",
    "/settings/ai-endpoint",
  );
  expect(screen.queryByRole("button", { name: /analyse/i })).toBeNull();
});

test("a failed analysis keeps the previous suggestions and says why; STOP WAITING only stops waiting", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  vmocked.templateSuggestions.mockResolvedValue({ result: SUGGESTION_RESULT, configured: true });
  vmocked.suggestTemplates.mockRejectedValueOnce(new ApiError(502, "Analysis failed — endpoint returned HTTP 502"));
  renderSuggested();
  await userEvent.click(await screen.findByRole("button", { name: /analyse again/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/HTTP 502.*previous suggestions are kept/i);
  expect(screen.getByText("Fix review notes")).toBeInTheDocument();

  vmocked.suggestTemplates.mockImplementationOnce(
    (signal?: AbortSignal) =>
      new Promise((_r, reject) => signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")))),
  );
  await userEvent.click(screen.getByRole("button", { name: /analyse again/i }));
  expect(await screen.findByText(/analysing your recent messages/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /stop waiting/i }));
  expect(await screen.findByText(/stopped waiting/i)).toBeInTheDocument();
  expect(screen.getByText("Fix review notes")).toBeInTheDocument();
});
