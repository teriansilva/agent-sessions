import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import type { Template } from "../../types/api";
import { TemplatePickerModal } from "./TemplatePickerModal";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      templates: vi.fn(),
      templateVariables: vi.fn(() => Promise.resolve({ variables: [], limits: {} })),
      sendTemplate: vi.fn(),
    },
  };
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

const mocked = api as unknown as {
  templates: ReturnType<typeof vi.fn>;
  templateVariables: ReturnType<typeof vi.fn>;
  sendTemplate: ReturnType<typeof vi.fn>;
};

/** A template whose host and command come from the variables library (#1090). */
function libTpl(): Template {
  return tpl({
    id: "smoke",
    name: "Smoke test",
    body: "Run {{test_cmd}} against {{host}} for {{ticket}}",
    fields: [
      { name: "test_cmd", label: "Test command", default: "", required: false, source: "library" },
      { name: "host", label: "Host", default: "", required: true, source: "library" },
      { name: "ticket", label: "Ticket", default: "", required: false, source: "template" },
    ],
    images: [],
  });
}

function library(values: Record<string, string>) {
  return {
    variables: Object.entries(values).map(([name, value]) => ({
      name,
      value,
      created_at: 1,
      updated_at: 1,
      used_by: [],
    })),
    limits: { variables_max: 100, value_max: 2000, name_max: 32 },
  };
}

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

// ---- the variables library (#1090) ---------------------------------------------------------

test("a library field starts at the library's value, says so, and can be changed for one send", async () => {
  mocked.templates.mockResolvedValue({ templates: [libTpl()], limits: LIMITS });
  mocked.templateVariables.mockResolvedValue(
    library({ test_cmd: "uv run pytest -q", host: "staging.acme.test" }),
  );
  const onSend = vi.fn();
  render(<TemplatePickerModal onSend={onSend} onInsert={vi.fn()} onClose={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: /smoke test/i }));
  expect(screen.getByText(/test command · from library/i)).toBeInTheDocument();
  expect(screen.getByLabelText(/^host$/i)).toHaveValue("staging.acme.test");
  expect(screen.getByLabelText(/what will be sent/i)).toHaveTextContent(
    "Run uv run pytest -q against staging.acme.test for",
  );
  // An override is for THIS send only — the picker has no way to write the library.
  const host = screen.getByLabelText(/^host$/i);
  await userEvent.clear(host);
  await userEvent.type(host, "canary.acme.test");
  await userEvent.click(screen.getByRole("button", { name: /^send smoke test$/i }));
  expect(onSend.mock.calls[0][1]).toEqual({
    test_cmd: "uv run pytest -q",
    host: "canary.acme.test",
    ticket: "",
  });
});

test("a missing library variable disables Send AND Insert and names the variable", async () => {
  mocked.templates.mockResolvedValue({ templates: [libTpl()], limits: LIMITS });
  mocked.templateVariables.mockResolvedValue(library({ host: "staging.acme.test" }));
  const onSend = vi.fn();
  const onInsert = vi.fn();
  render(<TemplatePickerModal onSend={onSend} onInsert={onInsert} onClose={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: /smoke test/i }));
  expect(screen.getByText(/test command · missing library variable/i)).toBeInTheDocument();
  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent("{{test_cmd}}");
  expect(within(alert).getByRole("link", { name: /variables/i })).toHaveAttribute(
    "href",
    "/templates?tab=variables",
  );
  // Typing a value into the empty slot does not unblock it: the library is the owner.
  await userEvent.type(screen.getByLabelText(/^test command$/i), "pytest");
  expect(screen.getByRole("button", { name: /^send smoke test$/i })).toBeDisabled();
  const insert = screen.getByRole("button", { name: /insert smoke test into composer/i });
  expect(insert).toBeDisabled();
  await userEvent.click(insert);
  expect(onSend).not.toHaveBeenCalled();
  expect(onInsert).not.toHaveBeenCalled();
});

test("a library that cannot be loaded never blocks templates without library fields", async () => {
  mocked.templates.mockResolvedValue({ templates: [tpl(), libTpl()], limits: LIMITS });
  mocked.templateVariables.mockRejectedValue(new Error("boom"));
  render(<TemplatePickerModal onSend={vi.fn()} onInsert={vi.fn()} onClose={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: /pr review checklist/i }));
  await userEvent.type(screen.getByLabelText(/^pr link$/i), "https://x/1");
  expect(screen.getByRole("button", { name: /^send pr review checklist$/i })).toBeEnabled();
  await userEvent.click(screen.getByRole("button", { name: /smoke test/i }));
  expect(screen.getByRole("alert")).toHaveTextContent(/couldn't load the variables library/i);
  expect(screen.getByRole("button", { name: /^send smoke test$/i })).toBeDisabled();
});

test("a multi-line library value reaches the send with its lines, and an override keeps them (#1095 review)", async () => {
  mocked.templates.mockResolvedValue({ templates: [libTpl()], limits: LIMITS });
  mocked.templateVariables.mockResolvedValue(
    library({ test_cmd: "cd repo\nnpm test", host: "staging.acme.test" }),
  );
  const onSend = vi.fn();
  render(<TemplatePickerModal onSend={onSend} onInsert={vi.fn()} onClose={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: /smoke test/i }));
  const cmd = screen.getByLabelText(/^test command$/i);
  expect(cmd).toHaveValue("cd repo\nnpm test");
  await userEvent.type(cmd, "{Enter}npm run lint");
  await userEvent.click(screen.getByRole("button", { name: /^send smoke test$/i }));
  expect(onSend.mock.calls[0][1].test_cmd).toBe("cd repo\nnpm test\nnpm run lint");
});

// ---- secret fields (#1090 Phase 2) -------------------------------------------------------------

function secretTpl(): Template {
  return tpl({
    id: "migrate",
    name: "Run migration",
    body: "Connect with {{db_pass}} and {{token}} for {{ticket}}",
    fields: [
      { name: "db_pass", label: "DB password", default: "", required: false, source: "library", kind: "secret" },
      { name: "token", label: "Deploy token", default: "", required: true, source: "template", kind: "secret" },
      { name: "ticket", label: "Ticket", default: "", required: true, source: "template", kind: "text" },
    ],
    images: [],
    updated_at: 7,
  });
}

function secretLibrary(state: "ok" | "reentry" | "absent" = "ok") {
  return {
    variables:
      state === "absent"
        ? []
        : [
            {
              name: "db_pass",
              kind: "secret",
              set: true,
              needs_reentry: state === "reentry",
              created_at: 1,
              updated_at: 1,
              used_by: [],
            },
          ],
    limits: { variables_max: 100, value_max: 2000, name_max: 32, secret_min: 8 },
  };
}

test("a secret template is sent by the server: stored secret never sent from here, typed one once, preview masked", async () => {
  mocked.templates.mockResolvedValue({ templates: [secretTpl()], limits: LIMITS });
  mocked.templateVariables.mockResolvedValue(secretLibrary());
  mocked.sendTemplate.mockResolvedValue({
    masked: "Connect with [secret: db_pass] and [secret: token] for ACME-7",
    template: secretTpl(),
  });
  const onSend = vi.fn();
  const onServerSent = vi.fn();
  render(
    <TemplatePickerModal
      sessionId="claude:abc"
      onSend={onSend}
      onServerSent={onServerSent}
      onInsert={vi.fn()}
      onClose={() => {}}
    />,
  );
  await userEvent.click(await screen.findByRole("button", { name: /run migration/i }));
  expect(screen.getByText("•••••••• stored")).toBeInTheDocument();
  const token = screen.getByLabelText(/^deploy token$/i);
  expect(token).toHaveAttribute("type", "password");
  await userEvent.type(token, "typed-token-1");
  await userEvent.type(screen.getByLabelText(/^ticket$/i), "ACME-7");
  // The preview never shows a secret — not even the one just typed.
  const preview = screen.getByLabelText(/what will be sent/i);
  expect(preview).toHaveTextContent("Connect with [secret: db_pass] and [secret: token] for ACME-7");
  expect(preview).not.toHaveTextContent("typed-token-1");
  // Insert would put the value in a text box: never offered.
  expect(screen.getByRole("button", { name: /insert run migration into composer/i })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: /^send run migration$/i }));
  expect(mocked.sendTemplate).toHaveBeenCalledWith(
    "migrate",
    "claude:abc",
    { token: "typed-token-1", ticket: "ACME-7" },
    7,
  );
  expect(onSend).not.toHaveBeenCalled();
  await waitFor(() => expect(onServerSent).toHaveBeenCalledTimes(1));
  expect(onServerSent.mock.calls[0][1].masked).toContain("[secret: token]");
});

test("a secret template cannot be sent from a session with no id yet, nor with a short or missing secret", async () => {
  mocked.templates.mockResolvedValue({ templates: [secretTpl()], limits: LIMITS });
  mocked.templateVariables.mockResolvedValue(secretLibrary());
  const { unmount } = render(
    <TemplatePickerModal sessionId={null} onSend={vi.fn()} onInsert={vi.fn()} onClose={() => {}} />,
  );
  await userEvent.click(await screen.findByRole("button", { name: /run migration/i }));
  await userEvent.type(screen.getByLabelText(/^deploy token$/i), "typed-token-1");
  await userEvent.type(screen.getByLabelText(/^ticket$/i), "T");
  const send = screen.getByRole("button", { name: /^send run migration$/i });
  expect(send).toBeDisabled();
  expect(screen.getByText(/can be sent once this session has started/i)).toBeInTheDocument();
  unmount();

  render(
    <TemplatePickerModal sessionId="claude:abc" onSend={vi.fn()} onInsert={vi.fn()} onClose={() => {}} />,
  );
  await userEvent.click(await screen.findByRole("button", { name: /run migration/i }));
  await userEvent.type(screen.getByLabelText(/^deploy token$/i), "short");
  await userEvent.type(screen.getByLabelText(/^ticket$/i), "T");
  expect(screen.getByRole("button", { name: /^send run migration$/i })).toBeDisabled();
  expect(mocked.sendTemplate).not.toHaveBeenCalled();
});

test("a stored secret that needs re-entry blocks the send and says why", async () => {
  mocked.templates.mockResolvedValue({ templates: [secretTpl()], limits: LIMITS });
  mocked.templateVariables.mockResolvedValue(secretLibrary("reentry"));
  render(
    <TemplatePickerModal sessionId="claude:abc" onSend={vi.fn()} onInsert={vi.fn()} onClose={() => {}} />,
  );
  await userEvent.click(await screen.findByRole("button", { name: /run migration/i }));
  expect(screen.getByText(/db password · secret · needs re-entry/i)).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent(/\{\{db_pass\}\} can no longer be decrypted/i);
  await userEvent.type(screen.getByLabelText(/^deploy token$/i), "typed-token-1");
  await userEvent.type(screen.getByLabelText(/^ticket$/i), "T");
  expect(screen.getByRole("button", { name: /^send run migration$/i })).toBeDisabled();
});

test("the mission brief (no session) can never insert a secret template", async () => {
  mocked.templates.mockResolvedValue({ templates: [secretTpl()], limits: LIMITS });
  mocked.templateVariables.mockResolvedValue(secretLibrary());
  const onInsert = vi.fn();
  render(<TemplatePickerModal insertLabel="Insert into mission brief" onInsert={onInsert} onClose={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: /run migration/i }));
  const insert = screen.getByRole("button", { name: /insert run migration into mission brief/i });
  expect(insert).toBeDisabled();
  expect(screen.getByText(/can only be sent into a session/i)).toBeInTheDocument();
  await userEvent.click(insert);
  expect(onInsert).not.toHaveBeenCalled();
});

test("a refused server-side send keeps the dialog open and shows the server's reason", async () => {
  mocked.templates.mockResolvedValue({ templates: [secretTpl()], limits: LIMITS });
  mocked.templateVariables.mockResolvedValue(secretLibrary());
  mocked.sendTemplate.mockRejectedValue(
    new ApiError(409, "This session isn't running — open it and try again"),
  );
  const onServerSent = vi.fn();
  render(
    <TemplatePickerModal
      sessionId="claude:abc"
      onSend={vi.fn()}
      onServerSent={onServerSent}
      onInsert={vi.fn()}
      onClose={() => {}}
    />,
  );
  await userEvent.click(await screen.findByRole("button", { name: /run migration/i }));
  await userEvent.type(screen.getByLabelText(/^deploy token$/i), "typed-token-1");
  await userEvent.type(screen.getByLabelText(/^ticket$/i), "T");
  await userEvent.click(screen.getByRole("button", { name: /^send run migration$/i }));
  expect(await screen.findByText(/isn't running/i)).toBeInTheDocument();
  expect(onServerSent).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toBeInTheDocument();
});

test("a server-side send in flight pins the dialog: Escape, backdrop and Close wait for it (#1105 review)", async () => {
  mocked.templates.mockResolvedValue({ templates: [secretTpl()], limits: LIMITS });
  mocked.templateVariables.mockResolvedValue(secretLibrary());
  let settle: (v: unknown) => void = () => {};
  mocked.sendTemplate.mockImplementation(() => new Promise((r) => (settle = r)));
  const onClose = vi.fn();
  const onServerSent = vi.fn();
  render(
    <TemplatePickerModal
      sessionId="claude:abc"
      onSend={vi.fn()}
      onServerSent={onServerSent}
      onInsert={vi.fn()}
      onClose={onClose}
    />,
  );
  await userEvent.click(await screen.findByRole("button", { name: /run migration/i }));
  await userEvent.type(screen.getByLabelText(/^deploy token$/i), "typed-token-1");
  await userEvent.type(screen.getByLabelText(/^ticket$/i), "T");
  await userEvent.click(screen.getByRole("button", { name: /^send run migration$/i }));
  await userEvent.keyboard("{Escape}");
  expect(screen.getByRole("button", { name: /^close$/i })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: /^close$/i }));
  expect(onClose).not.toHaveBeenCalled();
  await act(async () => settle({ masked: "m", template: secretTpl() }));
  expect(onServerSent).toHaveBeenCalledTimes(1);
  await userEvent.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("while a server send is pending, selection, Insert, field edits and the gallery links all wait (#1105 review, round 2)", async () => {
  mocked.templates.mockResolvedValue({
    templates: [secretTpl(), tpl({ id: "plain", name: "Plain one", fields: [], images: [] })],
    limits: LIMITS,
  });
  mocked.templateVariables.mockResolvedValue(secretLibrary());
  let settle: (v: unknown) => void = () => {};
  mocked.sendTemplate.mockImplementation(() => new Promise((r) => (settle = r)));
  const onInsert = vi.fn();
  const onOpenGallery = vi.fn();
  render(
    <TemplatePickerModal
      sessionId="claude:abc"
      onSend={vi.fn()}
      onServerSent={vi.fn()}
      onInsert={onInsert}
      onOpenGallery={onOpenGallery}
      onClose={vi.fn()}
    />,
  );
  await userEvent.click(await screen.findByRole("button", { name: /run migration/i }));
  await userEvent.type(screen.getByLabelText(/^deploy token$/i), "typed-token-1");
  await userEvent.type(screen.getByLabelText(/^ticket$/i), "T");
  await userEvent.click(screen.getByRole("button", { name: /^send run migration$/i }));
  // Another template cannot be chosen, the fields cannot change, and no link leaves.
  expect(screen.getByRole("button", { name: /^plain one/i })).toBeDisabled();
  expect(screen.getByLabelText(/^ticket$/i)).toHaveAttribute("readonly");
  await userEvent.type(screen.getByLabelText(/^ticket$/i), "X");
  expect(screen.getByLabelText(/^ticket$/i)).toHaveValue("T");
  await userEvent.click(screen.getByRole("link", { name: /manage in the gallery/i }));
  expect(onOpenGallery).not.toHaveBeenCalled();
  expect(onInsert).not.toHaveBeenCalled();
  await act(async () => settle({ masked: "m", template: secretTpl() }));
  await userEvent.click(screen.getByRole("link", { name: /manage in the gallery/i }));
  expect(onOpenGallery).toHaveBeenCalledWith("/templates");
});
