import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import type { Session, SessionsPage } from "../../types/api";
import { SessionList } from "./SessionList";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: { sessions: vi.fn(), rename: vi.fn(), archive: vi.fn(), unarchive: vi.fn() },
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
    project: "/home/m/claude",
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
