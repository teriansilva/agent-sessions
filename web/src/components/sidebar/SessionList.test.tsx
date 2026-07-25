import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../../app/config";
import { api, ApiError } from "../../lib/api";
import { projectColor } from "../../lib/format";
import type { AppConfig, Session, SessionsPage } from "../../types/api";
import { SessionList } from "./SessionList";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      sessions: vi.fn(),
      rename: vi.fn(),
      setTag: vi.fn(),
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

// The ⋯ menu's "Session brief" + "Hand off…" mount the terminal-header modals, keyed to the
// row's session (#597 follow-up). Those modals own their own API-driven internals and have
// their own suites; here we stub them with a sentinel dialog so we can assert SessionList's
// wiring — that the menu item mounts the right modal with the row's engine-qualified id.
vi.mock("../terminal/HandoffModal", () => ({
  HandoffModal: ({ sessionId, onClose }: { sessionId: string; onClose: () => void }) => (
    <div role="dialog" aria-label="handoff-mock" data-session={sessionId}>
      <button type="button" onClick={onClose}>
        close-handoff
      </button>
    </div>
  ),
}));
vi.mock("../terminal/SessionRecapModal", () => ({
  SessionRecapModal: ({ sessionId, onClose }: { sessionId: string; onClose: () => void }) => (
    <div role="dialog" aria-label="recap-mock" data-session={sessionId}>
      <button type="button" onClick={onClose}>
        close-recap
      </button>
    </div>
  ),
}));

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

/** Renders the current route so an imperative navigate() is assertable (#597). */
function LocationProbe() {
  return <span data-testid="loc">{useLocation().pathname}</span>;
}

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

test("an entity-assigned row keeps its project chip; the launch-folder chip is dropped (#508)", async () => {
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
  // The assigned row keeps its entity chip…
  const assignedRow = screen.getByRole("link", { name: /Assigned/ });
  expect(within(assignedRow).getByText(/Cayoo/)).toBeInTheDocument();
  // …but the launch-folder chip is gone (#508 declutter — meta line is engine · project · time).
  expect(within(assignedRow).queryByText(/~\/work\/api/)).not.toBeInTheDocument();
  // The unassigned row now shows neither an entity nor a folder path.
  const unassignedRow = screen.getByRole("link", { name: /Unassigned/ });
  expect(within(unassignedRow).queryByText(/Cayoo/)).not.toBeInTheDocument();
  expect(within(unassignedRow).queryByText(/~\/claude/)).not.toBeInTheDocument();
});

test("the project chip shows a color dot for a colored entity (#361), fed by the --proj var (#285)", async () => {
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
  // The row publishes the entity color as --proj (#285); the decorative dot consumes it.
  const row = screen.getByRole("link", { name: /Colored/ });
  expect(row.style.getPropertyValue("--proj")).toBe("#5fd7ff");
  const dot = container.querySelector('[class*="projectDot"]') as HTMLElement;
  expect(dot).not.toBeNull();
  expect(dot).toHaveAttribute("aria-hidden", "true");
  // #508: the folder chip + its decorative marker were removed — neither row shows a cwd path.
  expect(container.querySelector('[class*="folderMark"]')).toBeNull();
  expect(screen.queryByText(/~\/claude/)).not.toBeInTheDocument();
});

test("every row gets a stable project accent — explicit colour or the key hash — plus a rail on its own layer (#285)", async () => {
  const inProject = {
    ...sess("claude:p", "Assigned"),
    project: { kind: "project" as const, id: "p-2", name: "Two", color: "" },
  };
  mockSessions.mockResolvedValue(pageOf([inProject, sess("claude:q", "Unassigned")], { total: 2 }));
  render(
    <MemoryRouter initialEntries={["/s/claude/p"]}>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Assigned");
  // An uncoloured entity falls back to its id hash; a folder ref hashes its cwd.
  const assigned = screen.getByRole("link", { name: /Assigned/ });
  expect(assigned.style.getPropertyValue("--proj")).toBe(projectColor("p-2"));
  const unassigned = screen.getByRole("link", { name: /Unassigned/ });
  expect(unassigned.style.getPropertyValue("--proj")).toBe(projectColor("/home/m/claude"));
  // The rail is a decorative layer inside the row — and the active-session cue (#18)
  // still marks the open row independently of the project accent.
  expect(assigned.querySelector('[class*="projRail"]')).not.toBeNull();
  expect(assigned).toHaveAttribute("aria-current", "page");
  expect(unassigned).not.toHaveAttribute("aria-current");
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

// ---- Custom per-session tag (#551) ----

test("renders the tag before the AI summary; a tagged row with no summary still shows the tag (#551)", async () => {
  mockSessions.mockResolvedValue(
    pageOf(
      [
        { ...sess("claude:a", "Auth"), tag: "🔥 hot", ai_summary: "Wiring the pbkdf2 check" },
        { ...sess("claude:b", "Fresh"), tag: "todo" }, // tagged, not reviewed yet
      ],
      { total: 2 },
    ),
  );
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Auth");
  // The tag leads the summary line, joined by " · " (it renders BEFORE the summary).
  const tag = screen.getByText("🔥 hot");
  expect(tag.parentElement?.textContent).toBe("🔥 hot · Wiring the pbkdf2 check");
  // A tagged-but-unreviewed row still shows its tag, so the line is never empty.
  const todo = screen.getByText("todo");
  expect(todo.parentElement?.textContent).toBe("todo");
});

test("Set tag… opens the inline editor and saves via api.setTag, updating the row (#551)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  vi.mocked(api.setTag).mockResolvedValue({ id: "claude:a", tag: "prod" });
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: /set session tag/i }));
  const input = screen.getByRole("textbox", { name: /session tag/i });
  await user.type(input, "prod");
  await user.click(screen.getByRole("button", { name: /save tag/i }));
  expect(api.setTag).toHaveBeenCalledWith("claude:a", "prod");
  expect(await screen.findByText("prod")).toBeInTheDocument();
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

// ---- favorite, relocated into the ⋯ menu (#508, was the standalone star of #122) ----

test("favoriting from the row menu toggles sticky and shows the ★ prefix (#508)", async () => {
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
  // No standalone favorite button anymore — it lives in the ⋯ menu, and an unfavorited
  // row shows no ★ prefix.
  expect(screen.queryByRole("button", { name: "Favorite" })).not.toBeInTheDocument();
  expect(screen.queryByTitle("Favorited")).not.toBeInTheDocument();

  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: "Favorite session" }));
  expect(api.favorite).toHaveBeenCalledWith("claude:a");
  // The amber ★ prefix appears on the now-favorited row; re-opening the menu offers Unfavorite.
  expect(await screen.findByTitle("Favorited")).toBeInTheDocument();
  await openRowMenu(user);
  expect(screen.getByRole("menuitem", { name: "Unfavorite session" })).toBeInTheDocument();
  // Favoriting from the menu must not navigate the row.
  expect(onNavigate).not.toHaveBeenCalled();
  expect(screen.getByRole("link", { name: /First/ })).not.toHaveAttribute("aria-current");
});

test("a favorited row shows the ★ prefix and unfavorites from the menu (#508)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([{ ...sess("claude:a", "Pinned"), sticky: true }]));
  vi.mocked(api.unfavorite).mockResolvedValue({ id: "claude:a", sticky: false });
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Pinned");
  // Favorited rows lead the meta line with the decorative ★ (title="Favorited").
  expect(screen.getByTitle("Favorited")).toBeInTheDocument();

  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: "Unfavorite session" }));
  expect(api.unfavorite).toHaveBeenCalledWith("claude:a");
  // The ★ prefix clears once the row is unfavorited.
  await waitFor(() => expect(screen.queryByTitle("Favorited")).not.toBeInTheDocument());
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
    // 30s passes (one clockTick interval); relTime now sees the row as 80s old → "1 min ago".
    vi.setSystemTime(new Date(start.getTime() + 30_000));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(screen.getByText(/1 min ago/i)).toBeInTheDocument();
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
  // The fresh summary lands on the row AND flashes as the outcome toast (#392).
  expect(screen.getAllByText("Pipeline being refit")).toHaveLength(2);
});

// ---- Review-now outcome toast (#392) ----

test("Review now success toasts the fresh summary and is dismissible (#392)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  vi.mocked(api.reviewNow).mockResolvedValue({
    id: "claude:a",
    title: "First",
    ai_summary: "Deploy verified live",
    ai_title: "",
    intervention_required: false,
    intervention_reason: "",
    reviewed_at: Math.floor(Date.now() / 1000),
    review_excluded: false,
    ai_recap: "",
    recap_fingerprint: "",
  });
  renderWithAi(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: "Review session now" }));
  // The outcome lands in the polite live region so screen readers announce it too.
  const region = await screen.findByRole("status");
  await waitFor(() =>
    expect(within(region).getByText(/Deploy verified live/)).toBeInTheDocument(),
  );
  await user.click(within(region).getByRole("button", { name: /dismiss review result/i }));
  expect(within(region).queryByText(/Deploy verified live/)).not.toBeInTheDocument();
});

test("Review now failure toasts the server error detail and keeps the last good summary (#392)", async () => {
  const user = userEvent.setup();
  const now = Math.floor(Date.now() / 1000);
  mockSessions.mockResolvedValue(
    pageOf([
      {
        ...sess("claude:a", "First"),
        ai_summary: "Last good summary",
        reviewed_at: now,
        last_mtime: now,
      },
    ]),
  );
  // api.reviewNow surfaces the server's `detail` via ApiError (mutateJson) — the toast
  // must show THAT, not a generic "POST … → 502" (#392).
  vi.mocked(api.reviewNow).mockRejectedValue(
    new ApiError(502, "review endpoint timed out after 120s"),
  );
  renderWithAi(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Last good summary");
  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: "Review session now" }));
  const region = await screen.findByRole("status");
  await waitFor(() =>
    expect(
      within(region).getByText(/Review failed — review endpoint timed out after 120s/),
    ).toBeInTheDocument(),
  );
  // The failure never overwrites the last good row summary (#356 fail-soft holds).
  expect(screen.getByText("Last good summary")).toBeInTheDocument();
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
  // Lean menu: favorite + rename + archive — no AI-review items, no separator group.
  expect(screen.queryByRole("menuitem", { name: "Review session now" })).not.toBeInTheDocument();
  expect(
    screen.queryByRole("menuitem", { name: "Exclude from AI review" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "Favorite session" })).toBeInTheDocument();
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
// by separators between primary actions, AI-review actions, and row management. The
// leading "Session brief" + "Hand off…" mirror the terminal header (#597 follow-up).
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
    "Session brief",
    "Hand off…",
    "Review now",
    "Exclude from AI review",
    "Favorite",
    "Rename",
    "Set tag…",
    "Move to project…",
    "Archive",
  ]);
  expect(menu.querySelector('[role="separator"]')).not.toBeNull();
  // Exactly one trigger per row — the old four-button cluster is gone.
  expect(screen.getAllByRole("button", { name: "Session actions" })).toHaveLength(1);
});

// #597 follow-up: the header's "Session brief" + "Hand off…" are mirrored into the ⋯ menu so
// both are reachable from the sidebar without opening the session first. Each opens the same
// modal the header uses, keyed to the row's engine-qualified id.
test("the ⋯ menu opens the session brief for the row (#597 follow-up)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: "Open session brief" }));
  const dlg = await screen.findByRole("dialog", { name: "recap-mock" });
  expect(dlg).toHaveAttribute("data-session", "claude:a");
});

test("the ⋯ menu hands off the row's session to another engine (#597 follow-up)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("claude:a", "First")]));
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("First");
  await openRowMenu(user);
  await user.click(screen.getByRole("menuitem", { name: "Hand off session to another engine" }));
  const dlg = await screen.findByRole("dialog", { name: "handoff-mock" });
  expect(dlg).toHaveAttribute("data-session", "claude:a");
});

test("a shell session offers the brief but not Hand off (#597 follow-up)", async () => {
  const user = userEvent.setup();
  mockSessions.mockResolvedValue(pageOf([sess("shell:x", "Term", "shell")]));
  render(
    <MemoryRouter>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Term");
  await openRowMenu(user);
  expect(screen.getByRole("menuitem", { name: "Open session brief" })).toBeInTheDocument();
  expect(
    screen.queryByRole("menuitem", { name: "Hand off session to another engine" }),
  ).toBeNull();
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

// --- handoff provenance (#597 Phase 2) --------------------------------------------------

test("a handed-off-to row shows the inbound provenance badge", async () => {
  vi.mocked(api.sessions).mockResolvedValue(
    pageOf([{ ...sess("codex:t1", "Fix auth race", "codex"), handoff_from: "claude:s1" }]),
  );
  render(
    <MemoryRouter initialEntries={["/s/codex/t1"]}>
      <SessionList />
    </MemoryRouter>,
  );
  const badge = await screen.findByTitle(/handed off from claude · claude:s1/i);
  expect(badge).toHaveTextContent(/⇄ from cc/i);
});

test("a source row shows the muted outbound marker", async () => {
  vi.mocked(api.sessions).mockResolvedValue(
    pageOf([{ ...sess("claude:s1", "Fix auth race"), handoff_to: "codex:t1" }]),
  );
  render(
    <MemoryRouter initialEntries={["/s/claude/s1"]}>
      <SessionList />
    </MemoryRouter>,
  );
  const badge = await screen.findByTitle(/handed off to codex · codex:t1/i);
  expect(badge).toHaveTextContent(/⇄ to cx/i);
});

test("a row with no handoff shows no provenance badge", async () => {
  vi.mocked(api.sessions).mockResolvedValue(pageOf([sess("claude:a", "Plain session")]));
  render(
    <MemoryRouter initialEntries={["/s/claude/a"]}>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Plain session");
  expect(screen.queryByTitle(/handed off/i)).toBeNull();
});

test("the row menu routes to the handoff peer", async () => {
  const user = userEvent.setup();
  vi.mocked(api.sessions).mockResolvedValue(
    pageOf([{ ...sess("codex:t1", "Fix auth race", "codex"), handoff_from: "claude:s1" }]),
  );
  render(
    <MemoryRouter initialEntries={["/s/codex/t1"]}>
      <SessionList />
      <LocationProbe />
    </MemoryRouter>,
  );
  await screen.findByText("Fix auth race");
  const menu = await openRowMenu(user);
  // The backlink lives in the ⋯ menu, not nested in the row's NavLink (an anchor may not
  // contain interactive content) — tap-through per the issue's provenance goal.
  await user.click(within(menu).getByRole("menuitem", { name: /open the session this was handed off from/i }));
  expect(screen.getByTestId("loc")).toHaveTextContent("/s/claude/s1");
});

test("a row with no handoff has no peer menu item", async () => {
  const user = userEvent.setup();
  vi.mocked(api.sessions).mockResolvedValue(pageOf([sess("claude:a", "Plain session")]));
  render(
    <MemoryRouter initialEntries={["/s/claude/a"]}>
      <SessionList />
    </MemoryRouter>,
  );
  await screen.findByText("Plain session");
  const menu = await openRowMenu(user);
  expect(within(menu).queryByRole("menuitem", { name: /handed off/i })).toBeNull();
});

test("a chained session shows BOTH provenance relationships (#703 review)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.sessions).mockResolvedValue(
    pageOf([
      {
        ...sess("codex:mid", "Middle of a chain", "codex"),
        handoff_from: "claude:s1",
        handoff_to: "opencode:t2",
      },
    ]),
  );
  render(
    <MemoryRouter initialEntries={["/s/codex/mid"]}>
      <SessionList />
      <LocationProbe />
    </MemoryRouter>,
  );
  // `from || to` used to hide the outbound half of a chain — both must show.
  expect(await screen.findByTitle(/handed off from claude · claude:s1/i)).toBeVisible();
  expect(screen.getByTitle(/handed off to opencode · opencode:t2/i)).toBeVisible();
  // …and both peers are reachable.
  const menu = await openRowMenu(user);
  expect(within(menu).getByRole("menuitem", { name: /handed off from/i })).toBeVisible();
  await user.click(within(menu).getByRole("menuitem", { name: /handed off to/i }));
  expect(screen.getByTestId("loc")).toHaveTextContent("/s/opencode/t2");
});
