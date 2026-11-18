import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { SessionsProvider } from "../../app/SessionsContext";
import { ApiError, api } from "../../lib/api";
import { ARCHIVED_DETAIL } from "../../lib/archived";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo, Session, StructuredSnapshot } from "../../types/api";
import { StructuredPane } from "./StructuredPane";

// #2064: an archived API session is frozen. Its pane shows the history read-only, the ARCHIVED
// strip in place of the composer, and starts nothing — not on open, not on Unarchive.

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      structuredSnapshot: vi.fn(),
      structuredEvents: vi.fn(),
      structuredSubmit: vi.fn(),
      structuredStart: vi.fn(),
      structuredStop: vi.fn(),
      structuredSendNow: vi.fn(),
      structuredDecide: vi.fn(),
      session: vi.fn(),
      unarchive: vi.fn(),
      templates: vi.fn(() => Promise.resolve([])),
      uploadBlob: vi.fn(() => new Promise(() => {})),
    },
  };
});

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `codex-api:${ID}`;

function row(archived: boolean): Session {
  return {
    id: KEY,
    engine: "codex-api",
    uuid: ID,
    short_uuid: ID.slice(0, 8),
    cwd: "/w",
    project: { kind: "folder", id: "/w", name: "/w" },
    last_mtime: 1_700_000_000,
    title: "the session",
    first_user_message: "hello",
    sticky: false,
    archived,
  } as unknown as Session;
}

function snap(over: Partial<StructuredSnapshot> = {}): StructuredSnapshot {
  return {
    session_key: KEY,
    revision: 4,
    event_cursor: 4,
    cwd: "/w",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: "gpt-5-codex",
    turns: [
      {
        turn_id: "t1",
        operation_id: "t1",
        state: "completed",
        text: "so you work on this, right?",
        reply: "Yes. This session holds the claim.",
        text_truncated: false,
        reply_truncated: false,
        reason: null,
        tools: [],
        tools_truncated: false,
      },
    ],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n", worker: "w1", background_active: false },
    read_only: null,
    ...over,
  };
}

function renderPane() {
  return render(
    <MemoryRouter>
      <SessionsProvider>
        <StructuredPane engine="codex-api" id={ID} />
      </SessionsProvider>
    </MemoryRouter>,
  );
}

const composer = () => screen.queryByRole("textbox", { name: /message/i });
const unarchiveBtn = () => screen.getByTestId("archived-unarchive");

beforeEach(() => {
  setRoster(fixture.engines as EngineInfo[], []);
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap());
  vi.mocked(api.structuredEvents).mockResolvedValue({
    session_key: KEY,
    revision: 4,
    next_cursor: 4,
    events: [],
  });
});

afterEach(() => {
  vi.clearAllMocks();
  resetRoster();
});

test("an archived row: history stays, the strip replaces the composer, and nothing is started", async () => {
  vi.mocked(api.session).mockResolvedValue(row(true));
  renderPane();
  expect(await screen.findByTestId("archived-strip")).toBeInTheDocument();
  expect(await screen.findByText("so you work on this, right?")).toBeInTheDocument();
  expect(composer()).toBeNull();
  expect(screen.queryByTestId("structured-send")).toBeNull();
  expect(unarchiveBtn()).toHaveFocus();
  // The running-only head actions are gone; the read-side ones stay.
  expect(screen.queryByRole("button", { name: "Stop the worker" })).toBeNull();
  expect(screen.queryByRole("button", { name: /Hand off/ })).toBeNull();
  expect(screen.getByRole("button", { name: /plan and tools/i })).toBeInTheDocument();
  expect(api.structuredStart).not.toHaveBeenCalled();
  expect(api.structuredSubmit).not.toHaveBeenCalled();
});

test("the same session active shows the composer and Stop (the baseline the test above removes)", async () => {
  vi.mocked(api.session).mockResolvedValue(row(false));
  renderPane();
  expect(await screen.findByRole("textbox", { name: /message/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Stop the worker" })).toBeInTheDocument();
  expect(screen.queryByTestId("archived-strip")).toBeNull();
});

test("Unarchive: busy, then the composer once the re-read row reads active — and no start", async () => {
  const user = userEvent.setup();
  vi.mocked(api.session).mockResolvedValue(row(true));
  let finish!: () => void;
  vi.mocked(api.unarchive).mockImplementation(
    () => new Promise((resolve) => (finish = () => resolve({ id: KEY, archived: false }))),
  );
  renderPane();
  await screen.findByTestId("archived-strip");
  await user.click(unarchiveBtn());
  expect(api.unarchive).toHaveBeenCalledWith(KEY);
  expect(unarchiveBtn()).toHaveTextContent("Unarchiving…");
  expect(unarchiveBtn()).toBeDisabled();
  expect(composer()).toBeNull();
  vi.mocked(api.session).mockResolvedValue(row(false));
  await act(async () => finish());
  expect(await screen.findByRole("textbox", { name: /message/i })).toBeInTheDocument();
  expect(screen.queryByTestId("archived-strip")).toBeNull();
  expect(api.structuredStart).not.toHaveBeenCalled();
  expect(api.structuredSubmit).not.toHaveBeenCalled();
});

test("a failed Unarchive keeps the pane read-only and offers Retry with the server's reason", async () => {
  const user = userEvent.setup();
  vi.mocked(api.session).mockResolvedValue(row(true));
  vi.mocked(api.unarchive).mockRejectedValueOnce(
    new ApiError(409, "this session is held by a mission."),
  );
  renderPane();
  await screen.findByTestId("archived-strip");
  await user.click(unarchiveBtn());
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Couldn’t unarchive: this session is held by a mission. Still archived.",
  );
  expect(unarchiveBtn()).toHaveTextContent("Retry");
  expect(composer()).toBeNull();
  // Retry goes again; this time it lands.
  vi.mocked(api.unarchive).mockResolvedValueOnce({ id: KEY, archived: false });
  vi.mocked(api.session).mockResolvedValue(row(false));
  await user.click(unarchiveBtn());
  expect(await screen.findByRole("textbox", { name: /message/i })).toBeInTheDocument();
  expect(api.unarchive).toHaveBeenCalledTimes(2);
});

test("an Unarchive whose re-read row still says archived is a failure, not a composer", async () => {
  const user = userEvent.setup();
  vi.mocked(api.session).mockResolvedValue(row(true));
  vi.mocked(api.unarchive).mockResolvedValue({ id: KEY, archived: false });
  renderPane();
  await screen.findByTestId("archived-strip");
  await user.click(unarchiveBtn());
  expect(await screen.findByRole("alert")).toHaveTextContent(/still reads archived/);
  expect(composer()).toBeNull();
});

test("a send refused as archived (another device) freezes the pane and keeps the message", async () => {
  const user = userEvent.setup();
  vi.mocked(api.session).mockResolvedValue(row(false));
  vi.mocked(api.structuredSubmit).mockImplementation(async () => {
    // Another device archived it meanwhile: the row now reads archived.
    vi.mocked(api.session).mockResolvedValue(row(true));
    throw new ApiError(409, ARCHIVED_DETAIL);
  });
  renderPane();
  const box = await screen.findByRole("textbox", { name: /message/i });
  await waitFor(() => expect(screen.getByTestId("structured-send")).toBeDisabled());
  await user.type(box, "please run the suite");
  await waitFor(() => expect(screen.getByTestId("structured-send")).toBeEnabled());
  await user.click(screen.getByTestId("structured-send"));
  expect(await screen.findByTestId("archived-strip")).toBeInTheDocument();
  expect(composer()).toBeNull();
  // The unsent words are still on screen, in the outbox, refused with the server's reason.
  expect(screen.getByText("please run the suite")).toBeInTheDocument();
  expect(screen.getAllByText(new RegExp(ARCHIVED_DETAIL)).length).toBeGreaterThan(0);
  expect(api.structuredSubmit).toHaveBeenCalledTimes(1);
});
