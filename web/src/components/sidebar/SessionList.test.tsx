import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../../app/config";
import { api } from "../../lib/api";
import type { AppConfig, Session, SessionsPage } from "../../types/api";
import { SessionList } from "./SessionList";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      sessions: vi.fn(),
      rename: vi.fn(),
      archive: vi.fn(),
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

const mockSessions = vi.mocked(api.sessions);
const mockRename = vi.mocked(api.rename);
const mockArchive = vi.mocked(api.archive);

function sess(id: string, title: string, engine = "claude"): Session {
  return {
    id,
    engine,
    uuid: id.split(":")[1],
    short_uuid: id.slice(0, 8),
    cwd: "/home/m/claude",
    project: { kind: "folder" as const, id: "/home/m/claude", name: "/home/m/claude" },
    last_mtime: Math.floor(Date.now() / 1000),
    first_user_message: "",
    title,
    sticky: false,
    sort_key: 0,
    archived: false,
  };
}

function pageOf(sessions: Session[], over: Partial<SessionsPage> = {}): SessionsPage {
  return {
    sessions,
    next_offset: null,
    total: sessions.length,
    facets: { projects: [], engines: [] },
    ...over,
  };
}

beforeEach(() => {
  mockSessions.mockReset();
  mockRename.mockReset();
  mockArchive.mockReset();
  vi.mocked(api.favorite).mockReset();
  vi.mocked(api.unfavorite).mockReset();
  vi.mocked(api.projectEntities).mockReset();
  vi.mocked(api.setSessionProject).mockReset();
});

/** #384: row actions live behind a single ⋯ trigger now — open it first. */
async function openRowMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Session actions" }));
  return screen.getByRole("menu", { name: "Session actions" });
}

test("renders session rows from the API", async () => {
  mockSessions.mockResolvedValue(
    pageOf([sess("claude:a", "First"), sess("opencode:b", "Second", "opencode")], { total: 2 }),
  );
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  expect(await screen.findByText("First")).toBeInTheDocument();
  expect(screen.getByText("Second")).toBeInTheDocument();
});

// #284: when the server normalizes a meaningless auto-derived title to "", the row shows
// the (untitled) placeholder. The sidebar never consulted the raw first message — this
// pins that, so a stray "a" can't leak even if a future edit re-adds the fallback.
test("an empty (server-normalized) title shows the (untitled) placeholder, not the raw first message (#284)", async () => {
  mockSessions.mockResolvedValue(
    pageOf([{ ...sess("claude:u1abc", ""), first_user_message: "a" }], { total: 1 }),
  );
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  expect(await screen.findByText("(untitled)")).toBeInTheDocument();
  expect(screen.queryByText("a")).not.toBeInTheDocument();
});

test("a project-assigned row shows BOTH the entity name and its launch folder (#424 Phase 3)", async () => {
  const inProject = {
    ...sess("claude:p", "Assigned"),
    cwd: "/home/m/work/api",
    project: { kind: "project" as const, id: "p-1234", name: "Cayoo", color: "" },
  };
  mockSessions.mockResolvedValue(pageOf([inProject, sess("claude:q", "Unassigned")], { total: 2 }));
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Assigned");
  // The assigned row carries the entity chip AND its own folder chip (project + folder, #424).
  const assignedRow = screen.getByRole("link", { name: /Assigned/ });
  expect(within(assignedRow).getByText(/Cayoo/)).toBeInTheDocument();
  expect(within(assignedRow).getByText(/~\/work\/api/)).toBeInTheDocument();
  // The unassigned row has no entity → folder chip only, no Cayoo.
  const unassignedRow = screen.getByRole("link", { name: /Unassigned/ });
  expect(within(unassignedRow).queryByText(/Cayoo/)).not.toBeInTheDocument();
  expect(within(unassignedRow).getByText(/~\/claude/)).toBeInTheDocument();
});

test("the project chip shows a color dot for a colored entity; folder rows get a decorative marker (#361)", async () => {
  const colored = {
    ...sess("claude:p", "Colored"),
    project: { kind: "project" as const, id: "p-1", name: "Cayoo", color: "#5fd7ff" },
  };
  mockSessions.mockResolvedValue(pageOf([colored, sess("claude:q", "Plain")], { total: 2 }));
  const { container } = render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Colored");
  // The dot carries the entity color inline; it's decorative (aria-hidden).
  const dot = container.querySelector('[class*="projectDot"]') as HTMLElement;
  expect(dot).not.toBeNull();
  expect(dot.style.background).toBe("rgb(95, 215, 255)");
  expect(dot).toHaveAttribute("aria-hidden", "true");
  // The folder marker is a separate aria-hidden span, so the visible text content the
  // tests (and screen readers' name computation) rely on stays the project name/path.
  const mark = container.querySelector('[class*="folderMark"]') as HTMLElement;
  expect(mark).toHaveAttribute("aria-hidden", "true");
  // Both rows launch from the same cwd, so the folder chip now appears on each (#424 Phase 3).
  expect(screen.getAllByText(/~\/claude/)).toHaveLength(2);
});

test("marks the row matching the current URL as the active session (#18)", async () => {
  mockSessions.mockResolvedValue(
    pageOf([sess("claude:a", "First"), sess("opencode:b", "Second", "opencode")], { total: 2 }),
  );
  render(
    <MemoryRouter initialEntries={["/s/claude/a"]}>
      <SessionList />
    </MemoryRouter>,
  );
  // NavLink sets aria-current="page" on the row whose /s/:engine/:uuid matches the route.
  const open = await screen.findByRole("link", { name: /First/ });
  expect(open).toHaveAttribute("aria-current", "page");
  const other = screen.getByRole("link", { name: /Second/ });
  expect(other).not.toHaveAttribute("aria-current");
});

test("calls onNavigate when New session or a session row is tapped (#283 drawer-close)", async () => {
  const user = userEvent.setup();
  const onNavigate = vi.fn();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  render(
    <MemoryRouter initialEntries={["/s/claude/a"]}>
      <SessionList onNavigate={onNavigate} />
    </MemoryRouter>,
  );
  // The already-active row is a same-route no-op, so its onClick is what closes the drawer.
  await user.click(await screen.findByRole("link", { name: /First/ }));
  expect(onNavigate).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("link", { name: /new session/i }));
  expect(onNavigate).toHaveBeenCalledTimes(2);
});

test("shows the empty state when there are no sessions", async () => {
  mockSessions.mockResolvedValue(pageOf([]));
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  expect(await screen.findByText(/no sessions yet/i)).toBeInTheDocument();
});

test("renders a Load more control when there are more pages", async () => {
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")], { total: 5, next_offset: 1 }));
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  expect(await screen.findByRole("button", { name: /load more/i })).toBeInTheDocument();
});

test("renaming a row calls api.rename and updates the title in place", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "Old name")]));
  mockRename.mockResolvedValue({ id: "claude:a", title: "New name" });
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Old name");
  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: /rename session/i }));
  const input = screen.getByRole("textbox", { name: /session title/i });
  await user.clear(input);
  await user.type(input, "New name");
  await user.click(screen.getByRole("button", { name: /save title/i }));
  expect(mockRename).toHaveBeenCalledWith("claude:a", "New name");
  expect(await screen.findByText("New name")).toBeInTheDocument();
});

test("archiving a row calls api.archive and removes it from the active list", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "Doomed")], { total: 1 }));
  mockArchive.mockResolvedValue({ id: "claude:a", archived: true });
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Doomed");
  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: /archive session/i }));
  expect(mockArchive).toHaveBeenCalledWith("claude:a");
  await waitFor(() => expect(screen.queryByText("Doomed")).not.toBeInTheDocument());
});

// ---- favorite star (#122) ----

test("the favorite star toggles sticky via the API and flips its pressed state (#122)", async () => {
  const user = userEvent.setup();
  const onNavigate = vi.fn();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  vi.mocked(api.favorite).mockResolvedValue({ id: "claude:a", sticky: true });
  render(
    <MemoryRouter initialEntries={["/"]}>
      <SessionList onNavigate={onNavigate} />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  const star = screen.getByRole("button", { name: "Favorite" });
  expect(star).toHaveAttribute("aria-pressed", "false");

  await user.click(star);
  expect(api.favorite).toHaveBeenCalledWith("claude:a");
  // Flips to the pressed "Unfavorite" affordance once the row's sticky is set.
  const pressed = await screen.findByRole("button", { name: "Unfavorite" });
  expect(pressed).toHaveAttribute("aria-pressed", "true");
  // The star is a sibling of the row link — toggling must not navigate the row.
  expect(onNavigate).not.toHaveBeenCalled();
  expect(screen.getByRole("link", { name: /First/ })).not.toHaveAttribute("aria-current");
});

test("an already-favorited row shows a pressed star at rest and unfavorites (#122)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([{ ...sess("claude:a", "Pinned"), sticky: true }]));
  vi.mocked(api.unfavorite).mockResolvedValue({ id: "claude:a", sticky: false });
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Pinned");
  const star = screen.getByRole("button", { name: "Unfavorite" });
  expect(star).toHaveAttribute("aria-pressed", "true");

  await user.click(star);
  expect(api.unfavorite).toHaveBeenCalledWith("claude:a");
  expect(await screen.findByRole("button", { name: "Favorite" })).toBeInTheDocument();
});

// #156 / #211 4b: every row has a status LED, but only a working row carries the meaningful
// "agent working" status role — idle rows render a decorative (aria-hidden) dim dot.
test("only a working row exposes the 'agent working' status LED (#156)", async () => {
  const idle = sess("claude:a", "Idle");
  const busy = sess("claude:b", "Busy");
  busy.working = true;
  busy.last_output_at = Date.now() / 1000;
  mockSessions.mockResolvedValue(pageOf([idle, busy], { total: 2 }));
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Busy");
  const dots = await screen.findAllByRole("status", { name: /agent working/i });
  // One status LED — only the busy row carries it (the idle row's dot is aria-hidden).
  expect(dots).toHaveLength(1);
  // The LED leads the row link whose title is "Busy", not "Idle".
  expect(dots[0].closest("a")?.textContent).toMatch(/Busy/);
});

// #159: relative-time labels advance over time without a refetch — the sidebar shows
// "just now" when the row is fresh; 30s later (after the clockTick) it shows "1m ago".
test("relative-time labels advance over time without a refetch (#159)", async () => {
  const { act } = await import("@testing-library/react");
  const start = new Date("2026-05-28T08:00:00Z");
  const startSec = Math.floor(start.getTime() / 1000);
  const s = sess("claude:a", "First");
  s.last_mtime = startSec - 50; // 50s old → "just now"
  mockSessions.mockResolvedValue(pageOf([s], { total: 1 }));
  // shouldAdvanceTime lets the initial fetch's microtasks resolve while we still hold the
  // fake-timer steering wheel for the clockTick interval.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(start);
  try {
    render(
      <MemoryRouter>
        <SessionList />
      </MemoryRouter>,
    );
    await screen.findByText("First");
    expect(screen.getByText(/just now/i)).toBeInTheDocument();
    // 30s passes (one clockTick interval); relTime now sees the row as 80s old → "1m ago".
    vi.setSystemTime(new Date(start.getTime() + 30_000));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(screen.getByText(/1m ago/i)).toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

// ---- AI review surface (#356): summary line, badge, stale hint, row actions ----

const AI_CONFIG = {
  csrf: "t",
  new_session_engines: [],
  terminal_backend: "ws",
  ai_review: {
    enabled: true,
    base_url: "https://ai.example/v1",
    model: "m",
    interval_minutes: 5,
    prompt: "p",
    max_input_chars: 24000,
    api_key_set: true,
    configured: true,
    default_prompt: "p",
  },
} as unknown as AppConfig;

function renderWithAi(ui: React.ReactElement) {
  return render(<ConfigCtx.Provider value={AI_CONFIG}>{ui}</ConfigCtx.Provider>);
}

test("renders the AI summary line and the intervention badge with its reason (#356)", async () => {
  const now = Math.floor(Date.now() / 1000);
  mockSessions.mockResolvedValue(
    pageOf([
      {
        ...sess("claude:a", "Fix CI"),
        ai_summary: "Editing systemd limits; tests rerunning",
        intervention_required: true,
        intervention_reason: "waiting on permission prompt",
        reviewed_at: now,
        last_mtime: now,
      },
    ]),
  );
  renderWithAi(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  expect(await screen.findByText("Editing systemd limits; tests rerunning")).toBeInTheDocument();
  const badge = screen.getByRole("img", { name: /intervention required/i });
  expect(badge).toHaveAttribute("title", "waiting on permission prompt");
  // Fresh review (no newer activity) → no stale hint.
  expect(screen.queryByText(/· reviewed/)).not.toBeInTheDocument();
});

test("exposes the stale age when there has been activity since the last review (#356)", async () => {
  const now = Math.floor(Date.now() / 1000);
  mockSessions.mockResolvedValue(
    pageOf([
      {
        ...sess("claude:a", "Fix CI"),
        ai_summary: "All tests green",
        reviewed_at: now - 7200,
        last_mtime: now,
      },
    ]),
  );
  renderWithAi(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  expect(await screen.findByText(/All tests green/)).toBeInTheDocument();
  expect(screen.getByText(/· reviewed/)).toBeInTheDocument();
});

test("an excluded session shows the exclusion marker instead of a summary (#356)", async () => {
  mockSessions.mockResolvedValue(
    pageOf([
      {
        ...sess("claude:a", "rotate creds"),
        ai_summary: "should not show",
        intervention_required: true,
        review_excluded: true,
      },
    ]),
  );
  renderWithAi(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  expect(await screen.findByText("Excluded from AI review")).toBeInTheDocument();
  expect(screen.queryByText("should not show")).not.toBeInTheDocument();
  expect(screen.queryByRole("img", { name: /intervention required/i })).not.toBeInTheDocument();
});

test("Review now calls the API and folds the result into the row (#356)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "untitled work")]));
  vi.mocked(api.reviewNow).mockResolvedValue({
    id: "claude:a",
    title: "Refit the pipeline",
    ai_summary: "Pipeline being refit",
    ai_title: "Refit the pipeline",
    intervention_required: false,
    intervention_reason: "",
    reviewed_at: Math.floor(Date.now() / 1000),
    review_excluded: false,
  });
  renderWithAi(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("untitled work");
  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: "Review session now" }));
  expect(api.reviewNow).toHaveBeenCalledWith("claude:a");
  // Title precedence: the AI title becomes the display title for an untitled session.
  expect(await screen.findByText("Refit the pipeline")).toBeInTheDocument();
  expect(screen.getByText("Pipeline being refit")).toBeInTheDocument();
});

test("the exclude toggle flips review_excluded via the API (#356)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  vi.mocked(api.reviewExclude).mockResolvedValue({ id: "claude:a", review_excluded: true });
  renderWithAi(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: "Exclude from AI review" }));
  expect(api.reviewExclude).toHaveBeenCalledWith("claude:a", true);
  expect(await screen.findByText("Excluded from AI review")).toBeInTheDocument();
});

test("AI review menu items stay hidden while the endpoint is unconfigured (#356/#384)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  await openRowMenu(user);
  // Lean menu: rename + archive only — no AI-review items, no separator group.
  expect(screen.queryByRole("menuitem", { name: "Review session now" })).not.toBeInTheDocument();
  expect(
    screen.queryByRole("menuitem", { name: "Exclude from AI review" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "Rename session" })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "Archive session" })).toBeInTheDocument();
});

// #384: the ⋯ trigger sits beside the NavLink, so opening the menu must neither
// navigate the row nor fire the drawer-close callback.
test("opening the row menu does not navigate or close the drawer (#384)", async () => {
  const user = userEvent.setup();
  const onNavigate = vi.fn();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  render(
    <MemoryRouter initialEntries={["/"]}>
      <SessionList onNavigate={onNavigate} />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  await openRowMenu(user);
  expect(screen.getByRole("menu", { name: "Session actions" })).toBeInTheDocument();
  expect(onNavigate).not.toHaveBeenCalled();
  // The row link is still not aria-current — no navigation happened.
  expect(screen.getByRole("link", { name: /First/ })).not.toHaveAttribute("aria-current");
});

// #384: configured install — the full action set renders inside one menu, grouped
// by a separator between AI-review actions and row management.
test("the configured menu lists all actions behind one trigger (#384/#424)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  renderWithAi(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  const menu = await openRowMenu(user);
  const items = screen.getAllByRole("menuitem");
  expect(items.map((el) => el.textContent)).toEqual([
    "Review now",
    "Exclude from AI review",
    "Rename",
    "Move to project…",
    "Archive",
  ]);
  expect(menu.querySelector('[role="separator"]')).not.toBeNull();
  // Exactly one trigger per row — the old four-button cluster is gone.
  expect(screen.getAllByRole("button", { name: "Session actions" })).toHaveLength(1);
});

// #424 Phase 5b: the keyboard path for reassignment — "Move to project…" opens a picker that
// PATCHes the metadata seam and folds the entity into the row, mirroring the map's drag.
test("Move to project: picking an entity assigns it and updates the row (#424 Phase 5b)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  vi.mocked(api.projectEntities).mockResolvedValue({
    projects: [
      { id: "p-1", name: "Cayoo", color: "#5fd7ff", folders: [], archived: false, created_at: 0, session_count: 3 },
    ],
  });
  vi.mocked(api.setSessionProject).mockResolvedValue({ id: "claude:a", project_id: "p-1" });
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: "Move session to a project" }));

  const dialog = await screen.findByRole("dialog", { name: /move to project/i });
  await user.click(within(dialog).getByRole("button", { name: /Cayoo/ }));

  expect(api.setSessionProject).toHaveBeenCalledWith("claude:a", "p-1");
  // The row now carries the entity chip; the picker has closed.
  expect(await screen.findByText("Cayoo")).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: /move to project/i })).not.toBeInTheDocument(),
  );
});

test("Move to project: Escape closes the picker without assigning (#424 Phase 5b)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  vi.mocked(api.projectEntities).mockResolvedValue({ projects: [] });
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: "Move session to a project" }));
  await screen.findByRole("dialog", { name: /move to project/i });
  await user.keyboard("{Escape}");
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: /move to project/i })).not.toBeInTheDocument(),
  );
  expect(api.setSessionProject).not.toHaveBeenCalled();
});
