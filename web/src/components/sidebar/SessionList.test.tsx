import { render, screen, waitFor } from "@testing-library/react";
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
      reviewNow: vi.fn(),
      reviewExclude: vi.fn(),
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
});

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

test("a project-assigned row shows the entity name, not the folder path (#361)", async () => {
  const inProject = {
    ...sess("claude:p", "Assigned"),
    project: { kind: "project" as const, id: "p-1234", name: "Cayoo", color: "" },
  };
  mockSessions.mockResolvedValue(pageOf([inProject, sess("claude:q", "Unassigned")], { total: 2 }));
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Assigned");
  // entity name on the assigned row; the unassigned row keeps the shortened cwd
  expect(screen.getByText(/Cayoo/)).toBeInTheDocument();
  expect(screen.getByText(/~\/claude/)).toBeInTheDocument();
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
  expect(screen.getByText(/~\/claude/)).toBeInTheDocument();
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
  await user.click(await screen.findByRole("button", { name: /rename session/i }));
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
  await user.click(await screen.findByRole("button", { name: /archive session/i }));
  expect(mockArchive).toHaveBeenCalledWith("claude:a");
  await waitFor(() => expect(screen.queryByText("Doomed")).not.toBeInTheDocument());
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
  await user.click(screen.getByRole("button", { name: "Review session now" }));
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
  await user.click(screen.getByRole("button", { name: "Exclude from AI review" }));
  expect(api.reviewExclude).toHaveBeenCalledWith("claude:a", true);
  expect(await screen.findByText("Excluded from AI review")).toBeInTheDocument();
});

test("AI review row actions stay hidden while the endpoint is unconfigured (#356)", async () => {
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  expect(screen.queryByRole("button", { name: "Review session now" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Exclude from AI review" })).not.toBeInTheDocument();
});
