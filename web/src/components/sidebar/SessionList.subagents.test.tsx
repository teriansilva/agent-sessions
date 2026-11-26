/** Subagent sessions in the sidebar (#2325): the collapsed tree, the attention guarantee, the
 *  display mode and the archive prompt — against a mocked API. Layout, hit areas and real event
 *  dispatch are pinned by the Playwright spec (e2e/subagent-tree.spec.ts). */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import { mainRowStatus } from "../../lib/sessionStatus";
import type { Session, SessionLineagePage, SessionsPage } from "../../types/api";
import { SessionList } from "./SessionList";
import { readSubagentMode, SUBAGENT_MODE_KEY, writeSubagentMode } from "./subagentMode";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      sessions: vi.fn(),
      sessionChildren: vi.fn(),
      sessionAttention: vi.fn(),
      archive: vi.fn(),
      archiveWithSubagents: vi.fn(),
      rename: vi.fn(),
      setTag: vi.fn(),
      unarchive: vi.fn(),
      favorite: vi.fn(),
      unfavorite: vi.fn(),
      reviewNow: vi.fn(),
      reviewExclude: vi.fn(),
      projectEntities: vi.fn(),
      setSessionProject: vi.fn(),
    },
  };
});

const sessions = vi.mocked(api.sessions);
const children = vi.mocked(api.sessionChildren);
const attention = vi.mocked(api.sessionAttention);

function sess(id: string, title: string, over: Partial<Session> = {}): Session {
  return {
    id,
    engine: "claude",
    uuid: id.split(":")[1],
    short_uuid: id.slice(7, 15),
    cwd: "/home/m/claude",
    project: { kind: "folder", id: "/home/m/claude", name: "/home/m/claude" },
    last_mtime: Math.floor(Date.now() / 1000),
    first_user_message: "",
    title,
    sticky: false,
    archived: false,
    ...over,
  };
}

const page = (rows: Session[], over: Partial<SessionsPage> = {}): SessionsPage => ({
  sessions: rows,
  next_offset: null,
  total: rows.length,
  facets: { projects: [], engines: [] },
  ...over,
});

const lineage = (rows: Session[], total = rows.length, next: number | null = null): SessionLineagePage => ({
  sessions: rows,
  total,
  next_offset: next,
});

const MAIN = sess("claude:main-1", "Ship the fix", { child_count: 2, descendant_count: 3, rollup: "working" });
const KID_A = sess("claude:kid-a", "explore web/src", { parent_key: MAIN.id, working: true });
const KID_B = sess("claude:kid-b", "security review", { parent_key: MAIN.id });
const GRAND = sess("claude:grand", "lint pass", {
  intervention_required: true,
  intervention_reason: "approve rm",
  path: [
    { id: MAIN.id, title: "Ship the fix" },
    { id: KID_A.id, title: "explore web/src" },
  ],
});

beforeEach(() => {
  localStorage.clear();
  for (const m of [sessions, children, attention]) m.mockReset();
  vi.mocked(api.archiveWithSubagents).mockReset();
  vi.mocked(api.archive).mockReset();
});

function renderList() {
  return render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
}

test("a main session's subagents stay collapsed until the chip opens them", async () => {
  const user = userEvent.setup();
  sessions.mockResolvedValue(page([MAIN]));
  children.mockResolvedValue(lineage([KID_A, KID_B]));
  renderList();
  const chip = await screen.findByRole("button", { name: /show 2 subagents, working/i });
  expect(chip).toHaveAttribute("aria-expanded", "false");
  expect(children).not.toHaveBeenCalled();
  expect(screen.queryByText("explore web/src")).toBeNull();
  await user.click(chip);
  expect(await screen.findByText("explore web/src")).toBeInTheDocument();
  expect(screen.getByText("security review")).toBeInTheDocument();
  expect(children).toHaveBeenCalledWith(MAIN.id, { offset: 0, limit: 50, archived: false });
  expect(screen.getByRole("button", { name: /hide 2 subagents/i })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
});

test("a needs-you grandchild opens its main session, pinned first with its breadcrumb", async () => {
  const main = { ...MAIN, rollup: "attention" as const, attention_count: 1, attention: [GRAND.id] };
  sessions.mockResolvedValue(page([main]));
  attention.mockResolvedValue(lineage([GRAND]));
  children.mockResolvedValue(lineage([KID_A, KID_B]));
  renderList();
  const group = await screen.findByTestId("subagent-group");
  await within(group).findByText("lint pass");
  const rows = within(group).getAllByTestId("subagent-row");
  expect(rows[0]).toHaveTextContent("lint pass");
  expect(rows[0]).toHaveTextContent("↳ explore web/src");
  // The main row's own dot carries the bubble-up.
  expect(screen.getAllByRole("img", { name: /a subagent needs you/i }).length).toBeGreaterThan(0);
});

test("hidden mode shows only the subagents that need the operator", async () => {
  localStorage.setItem(SUBAGENT_MODE_KEY, "hidden");
  const main = { ...MAIN, rollup: "attention" as const, attention_count: 1, attention: [GRAND.id] };
  sessions.mockResolvedValue(page([main]));
  attention.mockResolvedValue(lineage([GRAND]));
  renderList();
  expect(await screen.findByText("lint pass")).toBeInTheDocument();
  expect(children).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: /show .* subagents/i })).toBeNull();
  expect(screen.getByTestId("subagent-count")).toHaveTextContent("2");
});

test("more than the first attention page pages through '+N more need you'", async () => {
  const user = userEvent.setup();
  const many = Array.from({ length: 12 }, (_, i) =>
    sess(`claude:need-${i}`, `need ${i}`, { intervention_required: true }),
  );
  const main = { ...MAIN, child_count: 12, rollup: "attention" as const, attention_count: 12 };
  sessions.mockResolvedValue(page([main]));
  children.mockResolvedValue(lineage([]));
  attention.mockImplementation(async (_id, q) =>
    q?.offset ? lineage(many.slice(10), 12, null) : lineage(many.slice(0, 10), 12, 10),
  );
  renderList();
  await screen.findByText("need 9");
  await user.click(screen.getByRole("button", { name: "+2 more need you" }));
  expect(await screen.findByText("need 11")).toBeInTheDocument();
});

test("a failed children read says so and retries", async () => {
  const user = userEvent.setup();
  sessions.mockResolvedValue(page([MAIN]));
  children.mockRejectedValueOnce(new Error("boom")).mockResolvedValue(lineage([KID_A]));
  renderList();
  await user.click(await screen.findByRole("button", { name: /show 2 subagents/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load subagents");
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByText("explore web/src")).toBeInTheDocument();
});

test("an empty children read says nothing is listed", async () => {
  const user = userEvent.setup();
  sessions.mockResolvedValue(page([MAIN]));
  children.mockResolvedValue(lineage([]));
  renderList();
  await user.click(await screen.findByRole("button", { name: /show 2 subagents/i }));
  expect(await screen.findByTestId("subagent-empty")).toBeInTheDocument();
});

test("a filtered-out main session's needs-you subagent is one tap away", async () => {
  const user = userEvent.setup();
  sessions.mockResolvedValue(
    page([], {
      attention_outside: {
        count: 1,
        mains: [{ id: MAIN.id, engine: "claude", title: "Ship the fix", attention_count: 1 }],
      },
    }),
  );
  attention.mockResolvedValue(lineage([GRAND]));
  renderList();
  const strip = await screen.findByTestId("attention-outside");
  expect(strip).toHaveTextContent("1 session needs you outside this filter");
  await user.click(within(strip).getByRole("button", { name: /show/i }));
  const row = await within(strip).findByText("lint pass");
  expect(row.closest("a")).toHaveAttribute("href", "/s/claude/grand");
  expect(within(strip).getByTestId("row-crumb")).toHaveTextContent(
    "↳ Ship the fix › explore web/src",
  );
});

test("every filtered-out main session is reachable from the strip, 20 at a time", async () => {
  const user = userEvent.setup();
  const mains = Array.from({ length: 51 }, (_, i) => ({
    id: `claude:out-${i}`,
    engine: "claude",
    title: `main ${i}`,
    attention_count: 1,
  }));
  sessions.mockResolvedValue(page([], { attention_outside: { count: 51, mains } }));
  attention.mockImplementation(async (id) =>
    lineage([sess(`claude:need-of-${id.split(":")[1]}`, `needs of ${id.split(":")[1]}`)]),
  );
  renderList();
  const strip = await screen.findByTestId("attention-outside");
  expect(strip).toHaveTextContent("51 sessions need you outside this filter");
  await user.click(within(strip).getByRole("button", { name: /outside this filter/ }));
  await within(strip).findByText("needs of out-19");
  expect(within(strip).queryByText("needs of out-20")).toBeNull();
  await user.click(within(strip).getByRole("button", { name: /31 more main sessions/ }));
  await within(strip).findByText("needs of out-39");
  await user.click(within(strip).getByRole("button", { name: /11 more main sessions/ }));
  expect(await within(strip).findByText("needs of out-50")).toBeInTheDocument();
  expect(within(strip).queryByRole("button", { name: /more main session/ })).toBeNull();
});

test("an orphan says where it came from", async () => {
  sessions.mockResolvedValue(page([sess("claude:o", "orphaned", { orphan: true })]));
  renderList();
  expect(await screen.findByTestId("row-crumb")).toHaveTextContent(
    "↳ from an archived or unlisted session",
  );
});

test("the sidebar has no display-mode control: it is chosen in Settings", async () => {
  sessions.mockResolvedValue(page([MAIN]));
  renderList();
  await screen.findByText("Ship the fix");
  expect(screen.queryByTestId("subagent-mode")).toBeNull();
  expect(screen.queryByRole("radio", { name: /hidden|nested/i })).toBeNull();
});

test("a mode chosen elsewhere (Settings, another tab) reaches the open list", async () => {
  sessions.mockResolvedValue(page([MAIN]));
  renderList();
  await screen.findByText("Ship the fix");
  expect(readSubagentMode()).toBe("nested");
  act(() => writeSubagentMode("hidden"));
  expect(readSubagentMode()).toBe("hidden");
  expect(await screen.findByTestId("subagent-count")).toHaveTextContent("2");
  expect(screen.queryByTestId("subagent-toggle")).toBeNull();
});

test("archive offers 'only this' by default and sends exactly the frozen subagent keys", async () => {
  const user = userEvent.setup();
  sessions.mockResolvedValue(page([MAIN]));
  children.mockImplementation(async (id) =>
    id === MAIN.id
      ? lineage([{ ...KID_A, child_count: 1 }, KID_B])
      : lineage([{ ...GRAND, path: undefined }]),
  );
  vi.mocked(api.archiveWithSubagents).mockResolvedValue({
    id: MAIN.id,
    results: [
      { id: MAIN.id, result: "archived", reason: null },
      { id: KID_A.id, result: "archived", reason: null },
      { id: GRAND.id, result: "failed", reason: "teardown failed" },
      { id: KID_B.id, result: "skipped", reason: "held by a mission" },
    ],
  });
  renderList();
  await screen.findByText("Ship the fix");
  await user.click(screen.getAllByRole("button", { name: "Session actions" })[0]);
  await user.click(screen.getByRole("menuitem", { name: /archive session/i }));
  const dialog = await screen.findByTestId("archive-subagents-dialog");
  const withSubs = await within(dialog).findByRole("button", { name: "Archive with 3 subagents" });
  expect(within(dialog).getByRole("button", { name: "Archive only this" })).toBeInTheDocument();
  await user.click(withSubs);
  expect(api.archiveWithSubagents).toHaveBeenCalledWith(MAIN.id, [KID_A.id, KID_B.id, GRAND.id]);
  const result = await screen.findByTestId("archive-subagents-result");
  expect(result).toHaveTextContent("2 of 4 done");
  expect(result).toHaveTextContent("Partial result. Nothing that failed was retried.");
  expect(result).toHaveTextContent("failed · teardown failed");
  expect(result).toHaveTextContent("skipped · held by a mission");
  expect(api.archive).not.toHaveBeenCalled();
  // The archived rows leave the list once the report is closed, not while it is being read.
  const mainLink = () => document.querySelector('a[href="/s/claude/main-1"]');
  expect(mainLink()).not.toBeNull();
  await user.click(within(result).getByRole("button", { name: "Done" }));
  await waitFor(() => expect(mainLink()).toBeNull());
});

test("archive only this archives the main session alone", async () => {
  const user = userEvent.setup();
  sessions.mockResolvedValue(page([MAIN]));
  children.mockResolvedValue(lineage([KID_A]));
  vi.mocked(api.archive).mockResolvedValue({ id: MAIN.id, archived: true });
  renderList();
  await screen.findByText("Ship the fix");
  await user.click(screen.getAllByRole("button", { name: "Session actions" })[0]);
  await user.click(screen.getByRole("menuitem", { name: /archive session/i }));
  const dialog = await screen.findByTestId("archive-subagents-dialog");
  await user.click(within(dialog).getByRole("button", { name: "Archive only this" }));
  await waitFor(() => expect(api.archive).toHaveBeenCalledWith(MAIN.id));
  expect(api.archiveWithSubagents).not.toHaveBeenCalled();
});

test("mainRowStatus lifts a calm row to attention, never masks its own reason", () => {
  expect(mainRowStatus({ rollup: "attention" }).label).toBe("a subagent needs you");
  expect(mainRowStatus({ rollup: "working" }).variant).toBe("idle");
  expect(
    mainRowStatus({ rollup: "attention", intervention_required: true, intervention_reason: "own" })
      .label,
  ).toBe("intervention required: own");
});
