import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, MemoryRouter, Route, Routes, RouterProvider } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError, setApiFetch } from "../lib/api";
import type { Template, TemplatesResponse } from "../types/api";
import Templates from "./Templates";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      templates: vi.fn(),
      createTemplate: vi.fn(),
      deleteTemplate: vi.fn(),
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
