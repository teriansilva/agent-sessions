import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import type { Template } from "../../types/api";
import { TemplatePickerModal } from "./TemplatePickerModal";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { templates: vi.fn() } };
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
    description: "Review a PR.",
    tags: ["review"],
    body: "Review PR {{pr_url}} for {{issue_ref}}",
    fields: [
      { name: "pr_url", label: "PR link", default: "", required: true },
      { name: "issue_ref", label: "Issue", default: "the linked issue", required: false },
    ],
    images: [{ name: "shot.png", path: "/u/.agent-sessions/uploads/20260903-1-shot.png" }],
    created_at: 1,
    updated_at: 2,
    used_count: 0,
    last_used_at: null,
    ...over,
  };
}

const mocked = api as unknown as { templates: ReturnType<typeof vi.fn> };

beforeEach(() => {
  vi.clearAllMocks();
});

test("lists the library, expands a template into its fill step, and gates SEND on required fields", async () => {
  mocked.templates.mockResolvedValue({
    templates: [tpl(), tpl({ id: "deploy", name: "Deploy watch", fields: [], images: [] })],
    limits: LIMITS,
  });
  const onSend = vi.fn();
  const onInsert = vi.fn();
  render(
    <TemplatePickerModal onSend={onSend} onInsert={onInsert} onClose={() => {}} />,
  );
  const list = await screen.findByRole("list", { name: /templates/i });
  expect(within(list).getAllByRole("listitem")).toHaveLength(2);
  await userEvent.click(screen.getByRole("button", { name: /pr review checklist/i }));
  const send = screen.getByRole("button", { name: /^send pr review checklist$/i });
  expect(send).toBeDisabled(); // pr_url is required and blank
  // The preview is the exact paste: default substituted, the blank slot empty, the path appended.
  const preview = screen.getByLabelText(/what will be sent/i);
  expect(preview).toHaveTextContent(
    "Review PR for the linked issue /u/.agent-sessions/uploads/20260903-1-shot.png",
  );
  await userEvent.type(screen.getByLabelText(/^pr link$/i), "https://x/1");
  expect(send).toBeEnabled();
  expect(preview).toHaveTextContent(
    "Review PR https://x/1 for the linked issue /u/.agent-sessions/uploads/20260903-1-shot.png",
  );
  await userEvent.click(send);
  expect(onSend).toHaveBeenCalledTimes(1);
  const [t, values] = onSend.mock.calls[0];
  expect(t.id).toBe("pr-review");
  expect(values).toEqual({ pr_url: "https://x/1", issue_ref: "the linked issue" });
  expect(onInsert).not.toHaveBeenCalled();
});

test("INSERT hands the same template and values to the composer", async () => {
  mocked.templates.mockResolvedValue({ templates: [tpl()], limits: LIMITS });
  const onInsert = vi.fn();
  render(<TemplatePickerModal onSend={vi.fn()} onInsert={onInsert} onClose={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: /pr review checklist/i }));
  await userEvent.click(screen.getByRole("button", { name: /insert pr review checklist into composer/i }));
  expect(onInsert).toHaveBeenCalledWith(
    expect.objectContaining({ id: "pr-review" }),
    { pr_url: "", issue_ref: "the linked issue" },
  );
});

test("preselect opens on that template with its defaults seeded; search narrows the list", async () => {
  mocked.templates.mockResolvedValue({
    templates: [
      tpl(),
      tpl({ id: "deploy", name: "Deploy watch", description: "Watch it", body: "Watch {{sha}}", tags: ["ops"], fields: [], images: [] }),
    ],
    limits: LIMITS,
  });
  render(
    <TemplatePickerModal preselect="deploy" onSend={vi.fn()} onInsert={vi.fn()} onClose={() => {}} />,
  );
  await screen.findByRole("list", { name: /templates/i });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /^deploy watch/i })).toHaveAttribute("aria-pressed", "true"),
  );
  expect(screen.getByRole("button", { name: /^send deploy watch$/i })).toBeEnabled();
  await userEvent.type(screen.getByRole("searchbox", { name: /search templates/i }), "review");
  expect(screen.getAllByRole("listitem")).toHaveLength(1);
  expect(screen.getByText("PR review checklist")).toBeInTheDocument();
});

test("an empty library is an honest empty state pointing at the gallery", async () => {
  mocked.templates.mockResolvedValue({ templates: [], limits: LIMITS });
  render(<TemplatePickerModal onSend={vi.fn()} onInsert={vi.fn()} onClose={() => {}} />);
  expect(await screen.findByText(/no templates yet/i)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /create one in the gallery/i })).toHaveAttribute(
    "href",
    "/templates",
  );
});

test("Escape closes", async () => {
  mocked.templates.mockResolvedValue({ templates: [tpl()], limits: LIMITS });
  const onClose = vi.fn();
  render(<TemplatePickerModal onSend={vi.fn()} onInsert={vi.fn()} onClose={onClose} />);
  await screen.findByRole("dialog");
  await userEvent.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalled();
});

test("focus is inside the dialog from the first paint: Close while loading, the search input once loaded (#908 review)", async () => {
  let resolveList: (v: { templates: Template[]; limits: typeof LIMITS }) => void = () => {};
  mocked.templates.mockImplementation(() => new Promise((r) => { resolveList = r; }));
  render(<TemplatePickerModal onSend={vi.fn()} onInsert={vi.fn()} onClose={() => {}} />);
  // Loading: no search input exists yet, and the page root is inert — Close holds focus.
  const close = await screen.findByRole("button", { name: /^close$/i });
  expect(close).toHaveFocus();
  await act(async () => {
    resolveList({ templates: [tpl()], limits: LIMITS });
  });
  // Loaded: the search input takes over.
  await waitFor(() => expect(screen.getByRole("searchbox", { name: /search templates/i })).toHaveFocus());
});

test("closing returns focus to a CONNECTED trigger: a detached one falls back to the More-keys button (#908 round 7)", async () => {
  mocked.templates.mockResolvedValue({ templates: [tpl()], limits: LIMITS });
  const root = document.createElement("div");
  root.id = "root";
  const more = document.createElement("button");
  more.setAttribute("aria-label", "More keys");
  root.appendChild(more);
  document.body.appendChild(root);
  // A real browser refuses focus inside an inert root: the restore must come AFTER the release.
  let inertAtFocus: boolean | null = null;
  more.addEventListener("focus", () => {
    inertAtFocus = root.hasAttribute("inert");
  });
  const detached = document.createElement("button"); // an overflow menu item, unmounted on click
  const { unmount } = render(
    <TemplatePickerModal onSend={vi.fn()} onInsert={vi.fn()} onClose={() => {}} returnFocusTo={detached} />,
  );
  await screen.findByRole("dialog");
  expect(root.hasAttribute("inert")).toBe(true);
  unmount();
  expect(more).toHaveFocus(); // unfixed: focus went to the detached node, i.e. document.body
  expect(inertAtFocus).toBe(false); // unfixed: restored while the root was still inert
  root.remove();
});
