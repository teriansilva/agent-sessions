/** ASK (#878; a page since #1058, the right-hand sidebar since #1294, durable since #1443) — the
 *  box's behaviour and the conversation's lifecycle.
 *
 *  Since #1443 the conversation is the server's (`/api/ask/*`): the console opens on an empty
 *  thread (a kept conversation is a switcher pick away), deletes one after a confirm, creates a
 *  conversation on its first question, streams a turn the server owns, and — when a
 *  stream ends early — reads the conversation again instead of declaring the turn lost. These tests
 *  drive that against a faked API: answers render with their matches, a refusal shows the server's
 *  detail, the head is the switcher + New conversation + Close, the switcher opens another
 *  conversation, a pending turn is read again until it settles, and late results never land in a
 *  conversation the operator has left.
 *
 *  The StrictMode case is the one that is red without `live.current = true` in the effect BODY:
 *  StrictMode cleans an effect up and re-runs it on the same fiber, and `useRef(true)` initialises
 *  once — so a cleanup-only liveness flag drops every answer in development.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import {
  WorkspaceCommandsCtx,
  type WorkspaceCommands,
} from "../../app/workspaceWindows";
import { beforeEach, expect, test, vi } from "vitest";

import { api, ApiError } from "../../lib/api";
import type {
  AppConfig,
  AskConversation,
  AskConversationTurn,
  AskNudgeCard,
  AskOperationCard,
  AskSessionCard,
  AskUpdate,
  AskStoredMatch,
  AskStoredMissionMatch,
  AskTurnEvent,
} from "../../types/api";

import { ConfigCtx } from "../../app/config";
import { OperationCard } from "./AskCards";
import { AskConsole } from "./AskConsole";
import { ASK_SEEN_EVENT, followRows } from "./askUpdates";
import { mapBatch } from "./askMatches";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      askConversations: vi.fn(),
      askConversation: vi.fn(),
      askNewConversation: vi.fn(),
      askDeleteConversation: vi.fn(),
      askMessageStream: vi.fn(),
      approveAction: vi.fn(),
      rejectAction: vi.fn(),
      decideAskProposal: vi.fn(),
      decideAskOperation: vi.fn(),
      askSeen: vi.fn(),
      askUnfollow: vi.fn(),
    },
  };
});

const CID = "ask_" + "1".repeat(32);
const OTHER = "ask_" + "2".repeat(32);

/** The console needs a router: a match row renders a `<Link>` into the session. */
function mount(ui: React.ReactNode) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

/** HQ opens on an empty conversation: mount, then open a kept one from the switcher, as the
 *  operator does. `index` is its row, latest first. */
async function mountAt(ui: React.ReactNode, index = 0) {
  const r = mount(ui);
  await pick(index);
  return r;
}

/** Open the switcher and pick row `index` (latest first). */
async function pick(index = 0) {
  await userEvent.click(screen.getByTestId("ask-switcher"));
  const items = await screen.findAllByTestId("ask-switcher-item");
  await userEvent.click(items[index]);
}

/** The console's own mount/unmount, driven from a control. */
function Host() {
  const [open, setOpen] = useState(true);
  return (
    <MemoryRouter>
      <button type="button" onClick={() => setOpen(false)}>
        leave
      </button>
      <button type="button" onClick={() => setOpen(true)}>
        return
      </button>
      {open ? <AskConsole configured /> : <div data-testid="gone" />}
    </MemoryRouter>
  );
}

interface Result {
  answer: string;
  matches?: AskStoredMatch[];
  mission_matches?: AskStoredMissionMatch[];
  status?: AskConversationTurn["status"];
  detail?: string;
}

function turnOf(question: string, turnId: string, r: Result): AskConversationTurn {
  const status = r.status ?? "done";
  return {
    turn_id: turnId,
    question,
    asked_at: 1,
    status,
    answer: status === "done" ? r.answer : null,
    detail: r.detail ?? null,
    code: null,
    matches: r.matches ?? [],
    mission_matches: r.mission_matches ?? [],
    tools: [],
  };
}

function conversation(id: string, turns: AskConversationTurn[] = []): AskConversation {
  return { id, title: turns[0]?.question ?? "", turns, running: false };
}

/** The server settles every question with `result`. */
function answerWith(result: Result) {
  vi.mocked(api.askMessageStream).mockImplementation(async (_id, body) => ({
    ...turnOf(body.text, body.turnId, result),
    answers: body.answers ?? null,
  }));
}

function twoConversations() {
  vi.mocked(api.askConversations).mockResolvedValue({
    conversations: [
      { id: CID, title: "the latest", created_at: 1, updated_at: 3 },
      { id: OTHER, title: "older", created_at: 1, updated_at: 2 },
    ],
    configured: true,
  });
  vi.mocked(api.askConversation).mockImplementation(async (id) =>
    conversation(id, [
      turnOf(id === CID ? "the latest" : "older", "t1", { answer: `answer for ${id}` }),
    ]),
  );
}

beforeEach(() => {
  vi.mocked(api.askMessageStream).mockReset();
  vi.mocked(api.askConversations).mockReset();
  vi.mocked(api.askConversation).mockReset();
  vi.mocked(api.askNewConversation).mockReset();
  vi.mocked(api.askDeleteConversation).mockReset();
  vi.mocked(api.approveAction).mockReset();
  vi.mocked(api.rejectAction).mockReset();
  vi.mocked(api.askConversations).mockResolvedValue({ conversations: [], configured: true });
  vi.mocked(api.askNewConversation).mockResolvedValue(conversation(CID));
  vi.mocked(api.askConversation).mockResolvedValue(conversation(CID));
  vi.mocked(api.askSeen).mockReset();
  vi.mocked(api.askUnfollow).mockReset();
  vi.mocked(api.askSeen).mockResolvedValue({ unread: false });
  vi.mocked(api.askUnfollow).mockResolvedValue({ following: false });
});

test("a question renders its answer in the thread (#522)", async () => {
  answerWith({ answer: "You merged it on Tuesday." });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "when did I merge it");
  await userEvent.click(screen.getByTestId("composer-send"));
  const turns = await screen.findByTestId("ask-turns");
  expect(await within(turns).findByText("You merged it on Tuesday.")).toBeInTheDocument();
});

test("a matched session is named, explained and reachable (#522)", async () => {
  // An answer that names a session the operator cannot open is half an answer — the match row
  // carries the reason it matched AND the way in.
  answerWith({
    answer: "Two sessions touched it.",
    matches: [{ id: "claude:abc-123", title: "fix flaky upload retry", why: "uploads.py" }],
  });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "upload retry");
  await userEvent.click(screen.getByTestId("composer-send"));
  const row = await screen.findByTestId("ask-match");
  expect(within(row).getByText("uploads.py")).toBeInTheDocument();
  expect(
    within(row).getByRole("link", { name: "Open fix flaky upload retry" }),
  ).toHaveAttribute("href", "/s/claude/abc-123");
});

test("a matched mission is named, explained and opens the mission (#1069)", async () => {
  const mid = "msn_" + "a".repeat(32);
  answerWith({
    answer: "The reconnect mission ran it.",
    matches: [{ id: "claude:abc-123", title: "ws backoff", why: "did the work" }],
    mission_matches: [
      {
        id: mid,
        title: "Stabilise terminal reconnects",
        state: "done",
        why: "the instruction names it",
      },
    ],
  });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "reconnect");
  await userEvent.click(screen.getByTestId("composer-send"));
  const row = await screen.findByTestId("ask-mission-match");
  expect(within(row).getByText("the instruction names it")).toBeInTheDocument();
  expect(
    within(row).getByRole("link", { name: "Open mission Stabilise terminal reconnects" }),
  ).toHaveAttribute("href", `/mission?m=${mid}`);
  // Both groups are labelled, missions first, and the session keeps its own way in.
  const turn = screen.getByTestId("ask-turn");
  const heads = Array.from(turn.querySelectorAll("div"))
    .map((d) => d.textContent)
    .filter((t) => t === "Missions" || t === "Sessions");
  expect(heads).toEqual(["Missions", "Sessions"]);
  expect(within(turn).getByRole("link", { name: "Open ws backoff" })).toHaveAttribute(
    "href",
    "/s/claude/abc-123",
  );
});

test("a sessions-only answer has no group labels, as before #1069", async () => {
  answerWith({ answer: "One session.", matches: [{ id: "claude:abc-123", title: "t", why: "w" }] });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "q");
  await userEvent.click(screen.getByTestId("composer-send"));
  await screen.findByTestId("ask-match");
  expect(screen.queryByText("Missions")).toBeNull();
  expect(screen.queryByText("Sessions")).toBeNull();
});

test("a busy 409 surfaces the server's detail, not a generic error (#522)", async () => {
  // The detail IS the answer here — "a question is already running" tells the operator to wait,
  // where "that didn't work" tells them to retry, which is the wrong move.
  vi.mocked(api.askMessageStream).mockRejectedValue(
    new ApiError(409, "a question is already running in this conversation"),
  );
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "hello");
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(await screen.findByTestId("ask-error")).toHaveTextContent(
    /a question is already running/i,
  );
});

test("a follow-up goes to the same conversation, created by the first question (#1443)", async () => {
  // History is the server's now: it replays the conversation's settled turns itself, so the
  // client sends the question alone — to the conversation it created on the first one.
  answerWith({ answer: "First answer." });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "one");
  await userEvent.click(screen.getByTestId("composer-send"));
  await screen.findByText("First answer.");
  answerWith({ answer: "Second answer." });
  await userEvent.type(screen.getByTestId("composer-input"), "two");
  await userEvent.click(screen.getByTestId("composer-send"));
  await screen.findByText("Second answer.");
  expect(api.askNewConversation).toHaveBeenCalledTimes(1);
  const calls = vi.mocked(api.askMessageStream).mock.calls;
  expect(calls.map((c) => [c[0], c[1].text])).toEqual([
    [CID, "one"],
    [CID, "two"],
  ]);
  expect(calls[0][1].turnId).not.toBe(calls[1][1].turnId);
});

test("an unconfigured endpoint disables the control and makes no call (#522)", async () => {
  mount(<AskConsole configured={false} />);
  expect(screen.getByTestId("composer-input")).toBeDisabled();
  expect(screen.getByTestId("composer-send")).toBeDisabled();
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(api.askMessageStream).not.toHaveBeenCalled();
  expect(api.askNewConversation).not.toHaveBeenCalled();
});

test('the thread carries no "answers are not kept" notice (operator request)', async () => {
  answerWith({ answer: "ok" });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "x");
  await userEvent.click(screen.getByTestId("composer-send"));
  await screen.findByTestId("ask-answer");
  expect(screen.queryByTestId("ask-transient")).toBeNull();
  expect(screen.queryByText(/not kept/i)).toBeNull();
});

test("leaving never cancels the turn: coming back starts empty, and opening the conversation shows it settled (#1443)", async () => {
  let settle: ((t: AskConversationTurn) => void) | undefined;
  let watching: AbortSignal | undefined;
  vi.mocked(api.askMessageStream).mockImplementation(
    (_id, _body, _on, signal) =>
      new Promise<AskConversationTurn>((res) => {
        watching = signal;
        settle = res;
      }),
  );
  render(<Host />);
  await userEvent.type(screen.getByTestId("composer-input"), "for this visit");
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(screen.getAllByTestId("ask-turn")).toHaveLength(1);
  await waitFor(() => expect(api.askMessageStream).toHaveBeenCalled());

  // The operator leaves: the page stops WATCHING — the server's turn goes on.
  await userEvent.click(screen.getByRole("button", { name: "leave" }));
  await waitFor(() => expect(watching?.aborted).toBe(true));
  const [, body] = vi.mocked(api.askMessageStream).mock.calls[0];
  const done = turnOf(body.text, body.turnId, { answer: "It settled while you were away." });
  settle?.(done);

  // Coming back starts on an empty conversation; the kept one is in the switcher, and reads the
  // server: the settled answer.
  vi.mocked(api.askConversations).mockResolvedValue({
    conversations: [{ id: CID, title: "for this visit", created_at: 1, updated_at: 2 }],
    configured: true,
  });
  vi.mocked(api.askConversation).mockResolvedValue(conversation(CID, [done]));
  await userEvent.click(screen.getByRole("button", { name: "return" }));
  expect(screen.queryByTestId("ask-turns")).toBeNull();
  await pick(0);
  expect(await screen.findByText("It settled while you were away.")).toBeInTheDocument();
  expect(screen.getAllByTestId("ask-turn")).toHaveLength(1);
});

test("StrictMode's effect re-run does not make the page drop every answer", async () => {
  // RED without `live.current = true` in the effect BODY (module note).
  answerWith({ answer: "It still answers." });
  render(
    <StrictMode>
      <MemoryRouter>
        <AskConsole configured />
      </MemoryRouter>
    </StrictMode>,
  );
  await userEvent.type(screen.getByTestId("composer-input"), "anything");
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(
    await within(await screen.findByTestId("ask-turns")).findByText("It still answers."),
  ).toBeInTheDocument();
});

// ---- #1171: Enter sends, the wait is shown ----------------------------------------------------

test("Enter sends; Shift+Enter is a new line; an IME Enter is the composition's", async () => {
  answerWith({ answer: "ok" });
  mount(<AskConsole configured />);
  const input = screen.getByTestId("composer-input");
  await userEvent.type(input, "line one{Shift>}{Enter}{/Shift}line two");
  expect(api.askMessageStream).not.toHaveBeenCalled();
  expect(input).toHaveValue("line one\nline two");
  // Confirming an IME composition fires Enter with isComposing — never a send.
  fireEvent.keyDown(input, { key: "Enter", isComposing: true });
  fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
  expect(api.askNewConversation).not.toHaveBeenCalled();
  await userEvent.type(input, "{Enter}");
  await waitFor(() => expect(api.askMessageStream).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.askMessageStream).mock.calls[0][1].text).toBe("line one\nline two");
  expect(input).toHaveValue("");
});

test("the wait names its step: the model, the tool it calls, the catalog, the transcripts", async () => {
  let emit: ((ev: AskTurnEvent) => void) | undefined;
  let finish: ((t: AskConversationTurn) => void) | undefined;
  vi.mocked(api.askMessageStream).mockImplementation(
    (_id, _body, onEvent) =>
      new Promise<AskConversationTurn>((res) => {
        emit = onEvent;
        finish = res;
      }),
  );
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "reconnect{Enter}");
  expect(screen.getByTestId("ask-working")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(/reading your question/i);
  await waitFor(() => expect(emit).toBeDefined());

  act(() => emit!({ type: "progress", step: "tool", tool: "find_sessions" }));
  expect(screen.getByRole("status")).toHaveTextContent("Searching your sessions and missions…");
  act(() => emit!({ type: "progress", step: "catalog", sessions: 42, missions: 1 }));
  expect(screen.getByRole("status")).toHaveTextContent("Searching 42 sessions and 1 mission…");
  act(() => emit!({ type: "progress", step: "content", candidates: 3 }));
  expect(screen.getByRole("status")).toHaveTextContent("Checking against 3 transcripts…");
  act(() => emit!({ type: "progress", step: "tool", tool: "session_status" }));
  expect(screen.getByRole("status")).toHaveTextContent("Reading a session's status…");
  act(() => emit!({ type: "progress", step: "thinking", round: 2 }));
  expect(screen.getByRole("status")).toHaveTextContent("Putting the answer together…");

  const [, body] = vi.mocked(api.askMessageStream).mock.calls[0];
  await act(async () =>
    finish!(
      turnOf(body.text, body.turnId, {
        answer: "It was the ws session.",
        matches: [{ id: "claude:abc", title: "ws", why: "transcript" }],
      }),
    ),
  );
  expect(await screen.findByText("It was the ws session.")).toBeInTheDocument();
  expect(screen.getByTestId("ask-match")).toBeInTheDocument();
  expect(screen.queryByTestId("ask-working")).toBeNull();
});

test("a turn the server failed shows its detail", async () => {
  answerWith({
    answer: "",
    status: "failed",
    detail: "the endpoint refused the request (HTTP 401)",
  });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "q{Enter}");
  expect(await screen.findByTestId("ask-error")).toHaveTextContent("HTTP 401");
  expect(screen.queryByTestId("ask-working")).toBeNull();
});

test("a stream cut mid-turn reads the conversation again and waits for the turn to settle (#1443)", async () => {
  vi.mocked(api.askMessageStream).mockRejectedValue(
    new ApiError(502, "The connection dropped before the answer finished."),
  );
  let reads = 0;
  vi.mocked(api.askConversation).mockImplementation(async () => {
    const [, body] = vi.mocked(api.askMessageStream).mock.calls[0];
    reads += 1;
    const t =
      reads === 1
        ? turnOf(body.text, body.turnId, { answer: "", status: "pending" })
        : turnOf(body.text, body.turnId, { answer: "Settled after the cut." });
    return conversation(CID, [t]);
  });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "where?{Enter}");
  // Still the server's turn: working, not an error, and the box waits for it.
  await waitFor(() => expect(reads).toBe(1));
  expect(screen.getByTestId("ask-working")).toBeInTheDocument();
  expect(screen.queryByTestId("ask-error")).toBeNull();
  expect(screen.getByTestId("composer-send")).toBeDisabled();
  // …then it is read again until it settles.
  expect(
    await screen.findByText("Settled after the cut.", {}, { timeout: 4000 }),
  ).toBeInTheDocument();
  expect(screen.queryByTestId("ask-working")).toBeNull();
  expect(screen.getAllByTestId("ask-turn")).toHaveLength(1);
});

test("the head is HQ, the switcher, Delete, New conversation and the sidebar's Close — nothing else (#1294, #1443, #1525)", async () => {
  answerWith({ answer: "ok" });
  const onClose = vi.fn();
  mount(<AskConsole configured onClose={onClose} />);
  const head = screen.getByTestId("ask-head");
  expect(within(head).getByRole("heading", { level: 2 })).toHaveTextContent("HQ");
  const switcher = within(head).getByTestId("ask-switcher");
  const remove = within(head).getByRole("button", { name: "Delete this conversation" });
  const fresh = within(head).getByRole("button", { name: "New conversation" });
  const close = within(head).getByRole("button", { name: "Close HQ" });
  expect(within(head).getAllByRole("button")).toEqual([switcher, remove, fresh, close]);
  // No way "back": Ask is not a page any more.
  expect(within(head).queryAllByRole("link")).toHaveLength(0);
  await userEvent.click(close);
  expect(onClose).toHaveBeenCalledTimes(1);
  // Nothing to start over from, or to delete, yet.
  expect(fresh).toBeDisabled();
  expect(remove).toBeDisabled();

  await userEvent.type(screen.getByTestId("composer-input"), "x{Enter}");
  await screen.findByTestId("ask-answer");
  await userEvent.click(fresh);
  expect(screen.queryByTestId("ask-turns")).toBeNull();
  expect(screen.getByTestId("composer-input")).toHaveFocus();
});

test("New conversation stops watching: the old turn's late progress never reaches the new thread", async () => {
  let emit: ((ev: AskTurnEvent) => void) | undefined;
  let signal: AbortSignal | undefined;
  vi.mocked(api.askMessageStream).mockImplementationOnce(
    (_id, _body, onEvent, s) =>
      new Promise<AskConversationTurn>((_res, rej) => {
        emit = onEvent;
        signal = s;
        s?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
      }),
  );
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "old question{Enter}");
  await waitFor(() => expect(emit).toBeDefined());

  await userEvent.click(screen.getByRole("button", { name: "New conversation" }));
  expect(signal?.aborted).toBe(true);
  // The new thread's first question creates a NEW conversation.
  vi.mocked(api.askNewConversation).mockResolvedValue(conversation(OTHER));
  answerWith({ answer: "new answer" });
  await userEvent.type(screen.getByTestId("composer-input"), "new question");
  expect(screen.getByTestId("composer-send")).toBeEnabled();
  await userEvent.type(screen.getByTestId("composer-input"), "{Enter}");
  expect(await screen.findByText("new answer")).toBeInTheDocument();
  expect(vi.mocked(api.askMessageStream).mock.calls[1][0]).toBe(OTHER);

  // A straggler from the old stream lands nowhere.
  act(() => emit!({ type: "progress", step: "tool", tool: "find_sessions" }));
  expect(screen.queryByText("old question")).toBeNull();
  expect(screen.getAllByTestId("ask-turn")).toHaveLength(1);
  expect(screen.queryByTestId("ask-working")).toBeNull();
  expect(screen.queryByTestId("ask-error")).toBeNull();
});

test("leaving the page stops watching the turn in flight", async () => {
  let signal: AbortSignal | undefined;
  vi.mocked(api.askMessageStream).mockImplementation(
    (_id, _body, _on, s) =>
      new Promise<AskConversationTurn>(() => {
        signal = s;
      }),
  );
  render(<Host />);
  await userEvent.type(screen.getByTestId("composer-input"), "q{Enter}");
  await waitFor(() => expect(signal).toBeDefined());
  expect(signal?.aborted).toBe(false);
  await userEvent.click(screen.getByRole("button", { name: "leave" }));
  await waitFor(() => expect(signal?.aborted).toBe(true));
});

test("a watch that settles LATE does not unlock the box under the next question", async () => {
  // The old stream ignores its abort and settles only after the next question has started — the
  // order a slow transport can produce. Only the CURRENT watch may clear `busy`.
  let settleOld: ((t: AskConversationTurn) => void) | undefined;
  vi.mocked(api.askMessageStream)
    .mockImplementationOnce(
      () =>
        new Promise<AskConversationTurn>((res) => {
          settleOld = res;
        }),
    )
    .mockImplementationOnce(() => new Promise<AskConversationTurn>(() => {})); // in flight
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "old{Enter}");
  await waitFor(() => expect(settleOld).toBeDefined());
  await userEvent.click(screen.getByRole("button", { name: "New conversation" }));
  await userEvent.type(screen.getByTestId("composer-input"), "new{Enter}");
  expect(screen.getByTestId("ask-working")).toBeInTheDocument();

  await act(async () => settleOld!(turnOf("old", "t-old", { answer: "late" })));
  await userEvent.type(screen.getByTestId("composer-input"), "third");
  // Still busy with "new": the late settle of "old" must not have re-enabled Send.
  expect(screen.getByTestId("composer-send")).toBeDisabled();
  expect(screen.getByTestId("ask-working")).toBeInTheDocument();
  expect(screen.queryByText("late")).toBeNull();
});

// --- the durable conversation (#1443) ---------------------------------------------------------

test("opening HQ starts on an empty conversation; the kept ones wait in the switcher", async () => {
  twoConversations();
  mount(<AskConsole configured />);
  await waitFor(() => expect(api.askConversations).toHaveBeenCalled());
  // Nothing is reopened on its own.
  await new Promise((r) => setTimeout(r, 50));
  expect(api.askConversation).not.toHaveBeenCalled();
  expect(screen.queryByTestId("ask-turns")).toBeNull();
  expect(screen.getByTestId("ask-switcher")).toHaveTextContent("New conversation");
  // …and the latest is one pick away.
  await pick(0);
  expect(await screen.findByText(`answer for ${CID}`)).toBeInTheDocument();
  expect(screen.getByTestId("ask-switcher")).toHaveTextContent("the latest");
});

test("Delete asks first; confirmed, it removes the conversation on screen and HQ starts empty", async () => {
  twoConversations();
  vi.mocked(api.askDeleteConversation).mockResolvedValue({ deleted: true, id: CID });
  await mountAt(<AskConsole configured />);
  await screen.findByText(`answer for ${CID}`);
  const remove = screen.getByRole("button", { name: "Delete this conversation" });
  expect(remove).toBeEnabled();

  // Cancel deletes nothing and hands focus back.
  await userEvent.click(remove);
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText("DELETE CONVERSATION")).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
  await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(api.askDeleteConversation).not.toHaveBeenCalled();
  expect(screen.getByText(`answer for ${CID}`)).toBeInTheDocument();

  // Confirmed: the server deletes THIS conversation; the thread is empty and the switcher no longer
  // lists it.
  vi.mocked(api.askConversations).mockResolvedValue({
    conversations: [{ id: OTHER, title: "older", created_at: 1, updated_at: 2 }],
    configured: true,
  });
  await userEvent.click(remove);
  await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(api.askDeleteConversation).toHaveBeenCalledWith(CID);
  expect(screen.queryByTestId("ask-turns")).toBeNull();
  expect(screen.queryByText(`answer for ${CID}`)).toBeNull();
  expect(remove).toBeDisabled();
  await userEvent.click(screen.getByTestId("ask-switcher"));
  const items = await screen.findAllByTestId("ask-switcher-item");
  expect(items.map((i) => i.textContent)).toEqual([expect.stringContaining("older")]);
});

test("a list read that predates a delete never puts the deleted conversation back (Hermes on #1564)", async () => {
  const both = {
    conversations: [
      { id: CID, title: "the latest", created_at: 1, updated_at: 3 },
      { id: OTHER, title: "older", created_at: 1, updated_at: 2 },
    ],
    configured: true,
  };
  const slow = deferred<typeof both>();
  vi.mocked(api.askConversations)
    .mockResolvedValueOnce(both) // mount
    .mockResolvedValueOnce(both) // the pick that opens CID
    .mockReturnValueOnce(slow.promise as never) // a refresh still out when the delete lands
    .mockResolvedValue({ conversations: [both.conversations[1]], configured: true });
  vi.mocked(api.askConversation).mockImplementation(async (id) =>
    conversation(id, [turnOf(id === CID ? "the latest" : "older", "t1", { answer: `answer for ${id}` })]),
  );
  vi.mocked(api.askDeleteConversation).mockResolvedValue({ deleted: true, id: CID });
  await mountAt(<AskConsole configured />);
  await screen.findByText(`answer for ${CID}`);
  // Open and close the switcher: its refresh is the slow one.
  await userEvent.click(screen.getByTestId("ask-switcher"));
  await userEvent.keyboard("{Escape}");
  await userEvent.click(screen.getByRole("button", { name: "Delete this conversation" }));
  await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  // The pre-delete read lands last, with the deleted conversation still in it.
  await act(async () => slow.resolve(both));
  vi.mocked(api.askConversations).mockReturnValue(new Promise(() => {}) as never); // freeze the list
  await userEvent.click(screen.getByTestId("ask-switcher"));
  const items = screen.getAllByTestId("ask-switcher-item");
  expect(items.map((i) => i.textContent)).toEqual([expect.stringContaining("older")]);
});

test("a refused delete says why in the dialog and keeps the conversation", async () => {
  twoConversations();
  vi.mocked(api.askDeleteConversation).mockRejectedValue(
    new ApiError(409, "HQ is still answering in this conversation — try again once it settles"),
  );
  await mountAt(<AskConsole configured />);
  await screen.findByText(`answer for ${CID}`);
  await userEvent.click(screen.getByRole("button", { name: "Delete this conversation" }));
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
  expect(await within(dialog).findByTestId("ask-delete-error")).toHaveTextContent(
    "still answering",
  );
  expect(screen.getByText(`answer for ${CID}`)).toBeInTheDocument();
});

test("the switcher lists latest first and opens the one picked — by keyboard", async () => {
  twoConversations();
  await mountAt(<AskConsole configured />);
  await screen.findByText(`answer for ${CID}`);
  const trigger = screen.getByTestId("ask-switcher");
  await userEvent.click(trigger);
  const items = screen.getAllByTestId("ask-switcher-item");
  expect(items.map((i) => i.textContent)).toEqual([
    expect.stringContaining("the latest"),
    expect.stringContaining("older"),
  ]);
  // Focus moved INTO the list, onto the conversation on screen.
  expect(items[0]).toHaveFocus();
  expect(items[0]).toHaveAttribute("aria-selected", "true");
  await userEvent.keyboard("{ArrowDown}");
  expect(items[1]).toHaveFocus();
  await userEvent.keyboard("{Enter}");
  expect(await screen.findByText(`answer for ${OTHER}`)).toBeInTheDocument();
  expect(screen.queryByText(`answer for ${CID}`)).toBeNull();
  expect(trigger).toHaveFocus();
});

test("Escape closes the switcher, hands focus back, and goes no further", async () => {
  twoConversations();
  const outer = vi.fn();
  render(
    <MemoryRouter>
      <div onKeyDown={outer}>
        <AskConsole configured />
      </div>
    </MemoryRouter>,
  );
  await pick(0);
  await screen.findByText(`answer for ${CID}`);
  const trigger = screen.getByTestId("ask-switcher");
  await userEvent.click(trigger);
  expect(screen.getByTestId("ask-switcher-menu")).toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByTestId("ask-switcher-menu")).toBeNull();
  expect(trigger).toHaveFocus();
  // The sidebar's own Escape (it closes Ask on a desktop) never heard it.
  expect(outer.mock.calls.filter(([e]) => e.key === "Escape")).toHaveLength(0);
});

test("a late answer for a conversation the operator switched away from lands nowhere", async () => {
  vi.mocked(api.askConversations).mockResolvedValue({
    conversations: [
      { id: CID, title: "first", created_at: 1, updated_at: 3 },
      { id: OTHER, title: "second", created_at: 1, updated_at: 2 },
    ],
    configured: true,
  });
  vi.mocked(api.askConversation).mockImplementation(async (id) => conversation(id, []));
  let finish: ((t: AskConversationTurn) => void) | undefined;
  vi.mocked(api.askMessageStream).mockImplementation(
    () =>
      new Promise<AskConversationTurn>((res) => {
        finish = res;
      }),
  );
  await mountAt(<AskConsole configured />);
  await waitFor(() => expect(api.askConversation).toHaveBeenCalledWith(CID));
  await userEvent.type(screen.getByTestId("composer-input"), "in the first{Enter}");
  await waitFor(() => expect(finish).toBeDefined());
  await userEvent.click(screen.getByTestId("ask-switcher"));
  await userEvent.click(screen.getAllByTestId("ask-switcher-item")[1]);
  await waitFor(() => expect(api.askConversation).toHaveBeenCalledWith(OTHER));
  const [, body] = vi.mocked(api.askMessageStream).mock.calls[0];
  await act(async () =>
    finish!(turnOf(body.text, body.turnId, { answer: "belongs to the first" })),
  );
  expect(screen.queryByText("belongs to the first")).toBeNull();
  expect(screen.queryByText("in the first")).toBeNull();
});

// --- Open / Open in map (operator request, 2026-10) ------------------------------------------

const M = (id: string, title = id) => ({ id, title, why: "" });

test("mapBatch: open windows always ride along; new ones only as far as the cap's room", () => {
  const ms = [M("claude:a"), M("claude:b"), M("claude:c"), M("claude:d")];
  const r = mapBatch(ms, new Set(["claude:c"]), 2);
  expect(r.take.map((m) => m.id)).toEqual(["claude:a", "claude:b", "claude:c"]);
  expect(r.left).toBe(1);
  expect(mapBatch(ms, new Set(), 0)).toEqual({ take: [], left: 4 });
  expect(mapBatch(ms, new Set(), 9).left).toBe(0);
});

function commands(over: Partial<WorkspaceCommands> = {}): WorkspaceCommands {
  return {
    mapReady: false,
    hostable: true,
    hasRoom: true,
    room: 8,
    openKeys: new Set(),
    requestOpen: vi.fn(),
    openInMap: vi.fn(() => false),
    ...over,
  };
}

function mountWithMap(cmds: WorkspaceCommands) {
  return render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <WorkspaceCommandsCtx.Provider value={cmds}>
        <Routes>
          <Route path="/overview" element={<div data-testid="on-map" />} />
          <Route path="*" element={<AskConsole configured />} />
        </Routes>
      </WorkspaceCommandsCtx.Provider>
    </MemoryRouter>,
  );
}

async function askTwo() {
  answerWith({
    answer: "Two sessions.",
    matches: [
      { id: "claude:abc", title: "upload retry", why: "" },
      { id: "codex:def", title: "ws backoff", why: "" },
    ],
  });
  await userEvent.type(screen.getByTestId("composer-input"), "q");
  await userEvent.click(screen.getByTestId("composer-send"));
  await screen.findAllByTestId("ask-match");
}

test("a match offers Open AND Open in map; Open in map queues a window and goes to the map", async () => {
  const cmds = commands();
  mountWithMap(cmds);
  await askTwo();
  const row = screen.getAllByTestId("ask-match")[1];
  expect(within(row).getByRole("link", { name: "Open ws backoff" })).toHaveAttribute(
    "href",
    "/s/codex/def",
  );
  await userEvent.click(within(row).getByRole("button", { name: "Open ws backoff in map" }));
  expect(cmds.requestOpen).toHaveBeenCalledTimes(1);
  expect(cmds.requestOpen).toHaveBeenCalledWith({
    key: "codex:def",
    engine: "codex",
    id: "def",
    title: "ws backoff",
  });
  expect(await screen.findByTestId("on-map")).toBeInTheDocument();
});

test("several matches offer Open all in map, which queues every one", async () => {
  const cmds = commands();
  mountWithMap(cmds);
  await askTwo();
  await userEvent.click(screen.getByTestId("ask-match-map-all"));
  expect(vi.mocked(cmds.requestOpen).mock.calls.map((c) => c[0].key)).toEqual([
    "claude:abc",
    "codex:def",
  ]);
});

test("Open all says how many fit when the cap is nearly full, and asks for no more", async () => {
  const cmds = commands({ room: 1 });
  mountWithMap(cmds);
  await askTwo();
  const all = screen.getByTestId("ask-match-map-all");
  expect(all).toHaveTextContent("Open 1 of 2 in map");
  await userEvent.click(all);
  expect(vi.mocked(cmds.requestOpen).mock.calls.map((c) => c[0].key)).toEqual(["claude:abc"]);
});

test("no window mode where the map cannot host one — Open stays", async () => {
  mountWithMap(commands({ hostable: false }));
  await askTwo();
  expect(screen.queryByTestId("ask-match-map")).toBeNull();
  expect(screen.queryByTestId("ask-match-map-all")).toBeNull();
  expect(screen.getAllByTestId("ask-match-open")).toHaveLength(2);
});

test("Open all is offered only once a map measurement said it can host — one window's refusal has a home, a batch's tail does not (Hermes on #1320)", async () => {
  mountWithMap(commands({ hostable: null }));
  await askTwo();
  expect(screen.getAllByTestId("ask-match-map")).toHaveLength(2);
  expect(screen.queryByTestId("ask-match-map-all")).toBeNull();
});

test("an answer read back from the server shows at once; only one that arrives live writes itself out", async () => {
  vi.stubGlobal(
    "matchMedia",
    (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }) as unknown as MediaQueryList,
  );
  try {
    twoConversations();
    answerWith({ answer: "a fresh answer that writes itself out" });
    await mountAt(<AskConsole configured />);
    const stored = await screen.findByTestId("ask-answer");
    expect(stored).not.toHaveAttribute("data-revealing");
    await userEvent.type(screen.getByTestId("composer-input"), "and now?{Enter}");
    await waitFor(() => expect(screen.getAllByTestId("ask-answer")).toHaveLength(2));
    expect(screen.getAllByTestId("ask-answer")[1]).toHaveAttribute("data-revealing", "true");
  } finally {
    vi.unstubAllGlobals();
  }
});

// --- Hermes on #1485: ordering ------------------------------------------------------------------

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

test("a question asked before the conversation list arrives goes to a NEW conversation, never a kept one", async () => {
  const list = deferred<{
    conversations: { id: string; title: string; created_at: number; updated_at: number }[];
    configured: boolean;
  }>();
  vi.mocked(api.askConversations).mockReturnValueOnce(list.promise as never);
  const earlier = turnOf("earlier", "t0", { answer: "the earlier answer" });
  vi.mocked(api.askConversation).mockImplementation(async (id) =>
    id === CID ? conversation(CID, [earlier]) : conversation(id, []),
  );
  vi.mocked(api.askNewConversation).mockResolvedValue(conversation(OTHER));
  answerWith({ answer: "kept" });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "first question{Enter}");
  // Shown at once, waiting — nothing sent before the selection is known.
  expect(within(screen.getByTestId("ask-turns")).getByText("first question")).toBeInTheDocument();
  expect(api.askMessageStream).not.toHaveBeenCalled();
  await act(async () =>
    list.resolve({
      conversations: [{ id: CID, title: "earlier", created_at: 1, updated_at: 2 }],
      configured: true,
    }),
  );
  // …then it goes to a conversation of its own: the kept one is never opened under it.
  expect(await screen.findByText("kept")).toBeInTheDocument();
  expect(screen.queryByText("the earlier answer")).toBeNull();
  expect(api.askNewConversation).toHaveBeenCalledTimes(1);
  expect(vi.mocked(api.askMessageStream).mock.calls[0][0]).toBe(OTHER);
  expect(screen.getAllByTestId("ask-turn")).toHaveLength(1);
});

test("a slow read never lands over a newer one: A → B → A keeps the second A", async () => {
  vi.mocked(api.askConversations).mockResolvedValue({
    conversations: [
      { id: CID, title: "a", created_at: 1, updated_at: 3 },
      { id: OTHER, title: "b", created_at: 1, updated_at: 2 },
    ],
    configured: true,
  });
  const firstA = deferred<AskConversation>();
  vi.mocked(api.askConversation)
    .mockReturnValueOnce(firstA.promise) // the initial open of A — slow
    .mockResolvedValueOnce(conversation(OTHER, [turnOf("b", "tb", { answer: "B answer" })]))
    .mockResolvedValueOnce(conversation(CID, [turnOf("a", "ta", { answer: "A, fresh" })]));
  await mountAt(<AskConsole configured />);
  await waitFor(() => expect(api.askConversation).toHaveBeenCalledTimes(1));
  await userEvent.click(screen.getByTestId("ask-switcher"));
  await userEvent.click(screen.getAllByTestId("ask-switcher-item")[1]);
  expect(await screen.findByText("B answer")).toBeInTheDocument();
  await userEvent.click(screen.getByTestId("ask-switcher"));
  await userEvent.click(screen.getAllByTestId("ask-switcher-item")[0]);
  expect(await screen.findByText("A, fresh")).toBeInTheDocument();
  await act(async () => firstA.resolve(conversation(CID, [turnOf("a", "ta", { answer: "A, stale" })])));
  expect(screen.queryByText("A, stale")).toBeNull();
  expect(screen.getByText("A, fresh")).toBeInTheDocument();
});

test("a question asked while the conversation's read is still out waits for it, then keeps its answer and shows the rest (#1776)", async () => {
  vi.mocked(api.askConversations).mockResolvedValue({
    conversations: [
      { id: CID, title: "a", created_at: 1, updated_at: 3 },
      { id: OTHER, title: "b", created_at: 1, updated_at: 2 },
    ],
    configured: true,
  });
  const slowB = deferred<AskConversation>();
  const earlier = turnOf("earlier in b", "tb0", { answer: "an earlier answer" });
  vi.mocked(api.askConversation)
    .mockResolvedValueOnce(conversation(CID, []))
    .mockReturnValueOnce(slowB.promise) // selecting B: slow
    .mockImplementation(async () => {
      const [, body] = vi.mocked(api.askMessageStream).mock.calls[0];
      return conversation(OTHER, [earlier, turnOf(body.text, body.turnId, { answer: "new answer" })]);
    });
  answerWith({ answer: "new answer" });
  await mountAt(<AskConsole configured />);
  await waitFor(() => expect(api.askConversation).toHaveBeenCalledWith(CID));
  await userEvent.click(screen.getByTestId("ask-switcher"));
  await userEvent.click(screen.getAllByTestId("ask-switcher-item")[1]);
  await userEvent.type(screen.getByTestId("composer-input"), "into b{Enter}");
  // Until B's read lands, a turn the server may already hold for B is unknown: nothing is sent
  // (#1776, Hermes review 7393 — the no-second-send invariant). The draft stays in the box.
  expect(api.askMessageStream).not.toHaveBeenCalled();
  expect(screen.getByTestId("composer-input")).toHaveValue("into b");
  await act(async () => slowB.resolve(conversation(OTHER, [earlier])));
  expect(await screen.findByText("an earlier answer")).toBeInTheDocument();
  await userEvent.type(screen.getByTestId("composer-input"), "{Enter}");
  expect(await screen.findByText("new answer")).toBeInTheDocument();
  // …and B's earlier turns are still shown beside the new answer.
  expect(screen.getByText("an earlier answer")).toBeInTheDocument();
  expect(vi.mocked(api.askMessageStream).mock.calls[0][0]).toBe(OTHER);
});

test("a refresh that reorders the open switcher: Enter opens the focused conversation", async () => {
  // The mount's read and the open that picks A see A first; every later read has B first.
  vi.mocked(api.askConversations)
    .mockResolvedValueOnce({
      conversations: [
        { id: CID, title: "a", created_at: 1, updated_at: 3 },
        { id: OTHER, title: "b", created_at: 1, updated_at: 2 },
      ],
      configured: true,
    })
    .mockResolvedValueOnce({
      conversations: [
        { id: CID, title: "a", created_at: 1, updated_at: 3 },
        { id: OTHER, title: "b", created_at: 1, updated_at: 2 },
      ],
      configured: true,
    })
    .mockResolvedValue({
      conversations: [
        { id: OTHER, title: "b", created_at: 1, updated_at: 9 },
        { id: CID, title: "a", created_at: 1, updated_at: 3 },
      ],
      configured: true,
    });
  vi.mocked(api.askConversation).mockImplementation(async (id) => conversation(id, []));
  await mountAt(<AskConsole configured />);
  await waitFor(() => expect(api.askConversation).toHaveBeenCalledWith(CID));
  await userEvent.click(screen.getByTestId("ask-switcher"));
  // The refresh opening triggered lands with B first; focus is on A's row, wherever it moved.
  await waitFor(() =>
    expect(screen.getAllByTestId("ask-switcher-item")[0]).toHaveTextContent("b"),
  );
  await waitFor(() =>
    expect(screen.getAllByTestId("ask-switcher-item")[1]).toHaveFocus(),
  );
  await userEvent.keyboard("{Enter}");
  expect(api.askConversation).not.toHaveBeenCalledWith(OTHER);
});

// --- #1443 Phase 2: asking back and nudges ---------------------------------------------------

const QID = "5e5e5e5e-0000-4000-8000-000000000001";
const AID = "a".repeat(32);

function askedTurn(answered = false, allowText = true): AskConversationTurn {
  return {
    ...turnOf("start something", "t-ask", { answer: "Which project?" }),
    ask: {
      question_id: QID,
      question: "Which project?",
      options: ["api", "web"],
      allow_text: allowText,
      answered,
    },
  };
}

function nudge(over: Partial<AskNudgeCard> = {}): AskNudgeCard {
  return {
    action_id: AID,
    kind: "nudge",
    key: "claude:abc",
    title: "api rebase",
    text: "Rebase onto main, then run the tests.",
    state: "proposed",
    projection: "actionable",
    can_approve: true,
    can_reject: true,
    detail: "",
    ...over,
  };
}

function withConversation(turns: AskConversationTurn[]) {
  vi.mocked(api.askConversations).mockResolvedValue({
    conversations: [{ id: CID, title: "x", created_at: 1, updated_at: 2 }],
    configured: true,
  });
  vi.mocked(api.askConversation).mockResolvedValue(conversation(CID, turns));
}

test("a question card answers by OPTION INDEX — the server sends the reply it stored", async () => {
  withConversation([askedTurn()]);
  answerWith({ answer: "ok, web" });
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-question");
  expect(within(card).getByText("Which project?", { selector: "p" })).toBeInTheDocument();
  await userEvent.click(within(card).getByRole("button", { name: "web" }));
  await screen.findByText("ok, web");
  const [id, body] = vi.mocked(api.askMessageStream).mock.calls[0];
  expect(id).toBe(CID);
  expect(body).toMatchObject({ text: "", answers: QID, option: 1 });
  // The settled answer names the question, so its card reads Answered without a reload.
  expect(within(card).getByText("Answered")).toBeInTheDocument();
  for (const b of within(card).getAllByTestId("ask-question-option")) expect(b).toBeDisabled();
});

test("free text answers when the question allows it", async () => {
  withConversation([askedTurn()]);
  answerWith({ answer: "noted" });
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-question");
  await userEvent.type(within(card).getByTestId("ask-question-text"), "the docs one");
  await userEvent.click(within(card).getByRole("button", { name: "Answer" }));
  await screen.findByText("noted");
  expect(vi.mocked(api.askMessageStream).mock.calls[0][1]).toMatchObject({
    text: "the docs one",
    answers: QID,
  });
  expect(vi.mocked(api.askMessageStream).mock.calls[0][1]).not.toHaveProperty("option");
});

test("an answered question offers nothing more, and a question without free text has no box", async () => {
  withConversation([askedTurn(true, false)]);
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-question");
  expect(within(card).getByText("Answered")).toBeInTheDocument();
  for (const b of within(card).getAllByTestId("ask-question-option")) expect(b).toBeDisabled();
  expect(within(card).queryByTestId("ask-question-text")).toBeNull();
});

test("a nudge card shows the exact text; Send posts the approve route and the card reads the outcome back", async () => {
  const turn = { ...turnOf("tell it to rebase", "t-n", { answer: "Proposed." }), proposals: [nudge()] };
  withConversation([turn]);
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-nudge");
  expect(within(card).getByTestId("ask-nudge-text")).toHaveTextContent(
    "Rebase onto main, then run the tests.",
  );
  vi.mocked(api.approveAction).mockResolvedValue({} as never);
  vi.mocked(api.askConversation).mockResolvedValue(
    conversation(CID, [
      { ...turn, proposals: [nudge({ state: "delivered", projection: "settled", can_approve: false, can_reject: false })] },
    ]),
  );
  await userEvent.click(within(card).getByRole("button", { name: "Send to api rebase" }));
  expect(api.approveAction).toHaveBeenCalledWith(AID);
  const outcome = await screen.findByTestId("ask-nudge-outcome");
  expect(outcome).toHaveTextContent("Delivered");
  expect(outcome).toHaveTextContent("That is not the agent having done it.");
  expect(screen.queryByTestId("ask-nudge-send")).toBeNull();
});

test("a refused approve (409 out of date) is an outcome the card reads back, not an error", async () => {
  const turn = { ...turnOf("tell it", "t-n", { answer: "Proposed." }), proposals: [nudge()] };
  withConversation([turn]);
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-nudge");
  vi.mocked(api.approveAction).mockRejectedValue(new ApiError(409, "the screen moved"));
  vi.mocked(api.askConversation).mockResolvedValue(
    conversation(CID, [
      { ...turn, proposals: [nudge({ state: "stale", projection: "settled", can_approve: false, can_reject: false })] },
    ]),
  );
  await userEvent.click(within(card).getByTestId("ask-nudge-send"));
  expect(await screen.findByTestId("ask-nudge-outcome")).toHaveTextContent(
    "Out of date",
  );
  expect(screen.getByTestId("ask-nudge-outcome")).toHaveTextContent("nothing was sent");
});

test("Dismiss posts the reject route", async () => {
  const turn = { ...turnOf("tell it", "t-n", { answer: "Proposed." }), proposals: [nudge()] };
  withConversation([turn]);
  vi.mocked(api.rejectAction).mockResolvedValue({} as never);
  await mountAt(<AskConsole configured />);
  await userEvent.click(await screen.findByTestId("ask-nudge-dismiss"));
  expect(api.rejectAction).toHaveBeenCalledWith(AID);
  expect(api.approveAction).not.toHaveBeenCalled();
});

test("a card whose ledger could not be read offers no control and says why", async () => {
  const turn = {
    ...turnOf("tell it", "t-n", { answer: "Proposed." }),
    proposals: [nudge({ state: null, projection: "unknown", can_approve: false, can_reject: false })],
  };
  withConversation([turn]);
  await mountAt(<AskConsole configured />);
  expect(await screen.findByTestId("ask-nudge-outcome")).toHaveTextContent("Status unavailable");
  expect(screen.queryByTestId("ask-nudge-send")).toBeNull();
});

// --- Hermes round 1 on #1489 -------------------------------------------------------------------

test("an open card follows a decision taken elsewhere (the app's action-resolved event)", async () => {
  const { ACTION_RESOLVED_EVENT } = await import("../../lib/actionEvents");
  const turn = { ...turnOf("tell it", "t-n", { answer: "Proposed." }), proposals: [nudge()] };
  withConversation([turn]);
  await mountAt(<AskConsole configured />);
  await screen.findByTestId("ask-nudge-send");
  vi.mocked(api.askConversation).mockResolvedValue(
    conversation(CID, [
      { ...turn, proposals: [nudge({ state: "rejected", projection: "settled", can_approve: false, can_reject: false })] },
    ]),
  );
  // Fired until it is heard (the listener is attached by a passive effect).
  await waitFor(() => {
    act(() => {
      window.dispatchEvent(new CustomEvent(ACTION_RESOLVED_EVENT));
    });
    expect(screen.getByTestId("ask-nudge-outcome")).toHaveTextContent("Dismissed");
  });
  expect(screen.queryByTestId("ask-nudge-send")).toBeNull();
});

test("an open card is re-read on a timer, so an expiry reaches it", async () => {
  // A short REAL interval rather than fake timers: Testing Library's own polling runs on the
  // faked clock too, which made this flaky on a loaded CI runner (#1489).
  const turn = { ...turnOf("tell it", "t-n", { answer: "Proposed." }), proposals: [nudge()] };
  withConversation([turn]);
  await mountAt(<AskConsole configured cardPollMs={40} />);
  await screen.findByTestId("ask-nudge-send");
  vi.mocked(api.askConversation).mockResolvedValue(
    conversation(CID, [
      { ...turn, proposals: [nudge({ state: "expired", projection: "settled", can_approve: false, can_reject: false })] },
    ]),
  );
  expect(
    await screen.findByTestId("ask-nudge-outcome", {}, { timeout: 5000 }),
  ).toHaveTextContent("Expired");
});

test.each(["dismissed", "expired"])("a decided card that earns no follow (%s) stops re-reading", async (state) => {
  const turn = {
    ...turnOf("tell it", "t-n", { answer: "Proposed." }),
    proposals: [nudge({ state, projection: "settled", can_approve: false, can_reject: false })],
  };
  withConversation([turn]);
  await mountAt(<AskConsole configured cardPollMs={20} />);
  await screen.findByTestId("ask-nudge-outcome");
  const reads = vi.mocked(api.askConversation).mock.calls.length;
  await new Promise((r) => setTimeout(r, 200)); // ten intervals' worth
  expect(vi.mocked(api.askConversation).mock.calls.length).toBe(reads);
});

test("a delivered card reads only for the bounded window its follow may take (#1517)", async () => {
  const turn = {
    ...turnOf("tell it", "t-n", { answer: "Proposed." }),
    proposals: [nudge({ state: "delivered", projection: "settled", can_approve: false, can_reject: false })],
  };
  withConversation([turn]);
  await mountAt(<AskConsole configured cardPollMs={20} reconcileMs={120} />);
  await screen.findByTestId("ask-nudge-outcome");
  await new Promise((r) => setTimeout(r, 300)); // past the window
  const reads = vi.mocked(api.askConversation).mock.calls.length;
  await new Promise((r) => setTimeout(r, 200));
  expect(vi.mocked(api.askConversation).mock.calls.length).toBe(reads);
});

test("an answer that failed leaves its question open to answer again", async () => {
  withConversation([askedTurn()]);
  answerWith({ answer: "", status: "failed", detail: "rate limited" });
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-question");
  await userEvent.click(within(card).getByRole("button", { name: "web" }));
  await screen.findByTestId("ask-error");
  expect(within(card).getByText("Waiting for you")).toBeInTheDocument();
  expect(within(card).getAllByTestId("ask-question-option")[0]).toBeEnabled();
});

// --- Hermes round 2 on #1489 -------------------------------------------------------------------

test("a decision that settles after a switch never blanks the conversation now on screen", async () => {
  vi.mocked(api.askConversations).mockResolvedValue({
    conversations: [
      { id: CID, title: "a", created_at: 1, updated_at: 3 },
      { id: OTHER, title: "b", created_at: 1, updated_at: 2 },
    ],
    configured: true,
  });
  const aTurn = { ...turnOf("tell it", "t-a", { answer: "Proposed." }), proposals: [nudge()] };
  const approve = deferred<never>();
  const bRead = deferred<AskConversation>();
  vi.mocked(api.askConversation).mockImplementation(async (id) =>
    id === CID ? conversation(CID, [aTurn]) : bRead.promise,
  );
  vi.mocked(api.approveAction).mockReturnValue(approve.promise);
  await mountAt(<AskConsole configured />);
  await userEvent.click(await screen.findByTestId("ask-nudge-send"));
  await userEvent.click(screen.getByTestId("ask-switcher"));
  await userEvent.click(screen.getAllByTestId("ask-switcher-item")[1]);
  await waitFor(() => expect(api.askConversation).toHaveBeenCalledWith(OTHER));
  // A's decision settles while B's read is still out…
  await act(async () => approve.resolve({} as never));
  // …and B's read still lands.
  await act(async () =>
    bRead.resolve(conversation(OTHER, [turnOf("b q", "t-b", { answer: "B's answer" })])),
  );
  expect(await screen.findByText("B's answer")).toBeInTheDocument();
  expect(vi.mocked(api.askConversation).mock.calls.filter(([id]) => id === CID)).toHaveLength(1);
});

test("a card whose ledger could not be read keeps re-reading until it can", async () => {
  const turn = {
    ...turnOf("tell it", "t-n", { answer: "Proposed." }),
    proposals: [nudge({ state: null, projection: "unknown", can_approve: false, can_reject: false })],
  };
  withConversation([turn]);
  await mountAt(<AskConsole configured cardPollMs={40} />);
  expect(await screen.findByTestId("ask-nudge-outcome")).toHaveTextContent("Status unavailable");
  vi.mocked(api.askConversation).mockResolvedValue(
    conversation(CID, [{ ...turn, proposals: [nudge()] }]),
  );
  expect(await screen.findByTestId("ask-nudge-send", {}, { timeout: 5000 })).toBeEnabled();
});

test("an approved-but-not-yet-sent message can still be dismissed", async () => {
  const turn = {
    ...turnOf("tell it", "t-n", { answer: "Proposed." }),
    proposals: [nudge({ state: "approved", projection: "in_flight_revocable", can_approve: false, can_reject: true })],
  };
  withConversation([turn]);
  vi.mocked(api.rejectAction).mockResolvedValue({} as never);
  await mountAt(<AskConsole configured />);
  expect(await screen.findByTestId("ask-nudge-outcome")).toHaveTextContent("Queued");
  expect(screen.queryByTestId("ask-nudge-send")).toBeNull();
  await userEvent.click(screen.getByTestId("ask-nudge-dismiss"));
  expect(api.rejectAction).toHaveBeenCalledWith(AID);
});

test("a message being typed (claimed) offers nothing", async () => {
  const turn = {
    ...turnOf("tell it", "t-n", { answer: "Proposed." }),
    proposals: [nudge({ state: "claimed", projection: "in_flight_locked", can_approve: false, can_reject: false })],
  };
  withConversation([turn]);
  await mountAt(<AskConsole configured />);
  expect(await screen.findByTestId("ask-nudge-outcome")).toHaveTextContent("Sending");
  expect(screen.queryByTestId("ask-nudge-dismiss")).toBeNull();
});

// --- Hermes round 3 on #1489 -------------------------------------------------------------------

test("a turn that staged a proposal and then failed still shows the live card", async () => {
  const turn = {
    ...turnOf("tell it", "t-n", { answer: "", status: "failed", detail: "the endpoint refused" }),
    proposals: [nudge()],
  };
  withConversation([turn]);
  await mountAt(<AskConsole configured />);
  expect(await screen.findByTestId("ask-error")).toHaveTextContent("the endpoint refused");
  expect(screen.getByTestId("ask-nudge-send")).toBeEnabled();
});

test("a free-text answer that failed keeps its draft to retry", async () => {
  withConversation([askedTurn()]);
  answerWith({ answer: "", status: "failed", detail: "rate limited" });
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-question");
  await userEvent.type(within(card).getByTestId("ask-question-text"), "the docs one");
  await userEvent.click(within(card).getByRole("button", { name: "Answer" }));
  await screen.findByTestId("ask-error");
  expect(within(card).getByTestId("ask-question-text")).toHaveValue("the docs one");
});

function geometry(el: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(el, "scrollHeight", { configurable: true, value: scrollHeight });
  Object.defineProperty(el, "clientHeight", { configurable: true, value: clientHeight });
}

test("a background re-read never pulls someone reading earlier turns to the bottom", async () => {
  const { ACTION_RESOLVED_EVENT } = await import("../../lib/actionEvents");
  const turn = { ...turnOf("tell it", "t-n", { answer: "Proposed." }), proposals: [nudge()] };
  withConversation([turn]);
  await mountAt(<AskConsole configured />);
  await screen.findByTestId("ask-nudge-send");
  const pane = screen.getByTestId("ask-pane");
  geometry(pane, 1000, 200);
  pane.scrollTop = 100; // reading earlier turns
  fireEvent.scroll(pane);
  const reads = vi.mocked(api.askConversation).mock.calls.length;
  // Fired until it is heard: the listener is attached by a passive effect, which a loaded runner
  // can run after the card is already on screen.
  await waitFor(() => {
    act(() => {
      window.dispatchEvent(new CustomEvent(ACTION_RESOLVED_EVENT));
    });
    expect(vi.mocked(api.askConversation).mock.calls.length).toBeGreaterThan(reads);
  });
  await act(async () => {});
  expect(pane.scrollTop).toBe(100);
});

test("…while someone already at the bottom keeps following it", async () => {
  const { ACTION_RESOLVED_EVENT } = await import("../../lib/actionEvents");
  const turn = { ...turnOf("tell it", "t-n", { answer: "Proposed." }), proposals: [nudge()] };
  withConversation([turn]);
  await mountAt(<AskConsole configured />);
  await screen.findByTestId("ask-nudge-send");
  const pane = screen.getByTestId("ask-pane");
  geometry(pane, 1000, 200);
  pane.scrollTop = 800; // at the bottom
  fireEvent.scroll(pane);
  geometry(pane, 1300, 200); // the re-read grows the thread
  const reads = vi.mocked(api.askConversation).mock.calls.length;
  // Fired until it is heard: the listener is attached by a passive effect, which a loaded runner
  // can run after the card is already on screen.
  await waitFor(() => {
    act(() => {
      window.dispatchEvent(new CustomEvent(ACTION_RESOLVED_EVENT));
    });
    expect(vi.mocked(api.askConversation).mock.calls.length).toBeGreaterThan(reads);
  });
  await waitFor(() => expect(pane.scrollTop).toBe(1300));
});

// --- #1443 Phase 3a: a session to start ---------------------------------------------------------

const PID = "b".repeat(32);
const BRIEF = "Rebase the api branch onto main and run the test suite.";

function sessionCard(over: Partial<AskSessionCard> = {}): AskSessionCard {
  return {
    action_id: PID,
    kind: "session",
    title: "api",
    agent: "Claude Code",
    text: BRIEF,
    state: "pending",
    engine: "claude",
    bypass_default: false,
    bypass: null,
    session_key: "",
    reason: "",
    notice: "",
    can_start: true,
    can_dismiss: true,
    ...over,
  };
}

function sessionTurn(card: AskSessionCard): AskConversationTurn {
  return { ...turnOf("start a session on api", "t-s", { answer: "Proposed." }), proposals: [card] };
}

const settled = (over: Partial<AskSessionCard>) =>
  sessionCard({ can_start: false, can_dismiss: false, ...over });

test("a session card shows the project, agent and exact brief, and the bypass starts at the default", async () => {
  withConversation([sessionTurn(sessionCard({ bypass_default: true }))]);
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-session");
  expect(card).toHaveTextContent("api");
  expect(card).toHaveTextContent("Claude Code");
  expect(within(card).getByTestId("ask-session-brief")).toHaveTextContent(BRIEF);
  const toggle = within(card).getByRole("switch");
  expect(toggle).toBeChecked();
  expect(within(card).getByTestId("ask-session-bypass")).toHaveTextContent(
    "Permission bypass · On",
  );
  expect(within(card).getByTestId("ask-session-bypass")).toHaveTextContent(
    "without asking you first",
  );
});

test("Start sends the decision and the bypass value the card shows — and nothing it starts", async () => {
  withConversation([sessionTurn(sessionCard())]);
  vi.mocked(api.decideAskProposal).mockResolvedValue({
    proposal: settled({ state: "deciding" }),
  });
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-session");
  await userEvent.click(within(card).getByRole("switch"));
  vi.mocked(api.askConversation).mockResolvedValue(
    conversation(CID, [
      sessionTurn(settled({ state: "started", bypass: true, session_key: "claude:abc-1" })),
    ]),
  );
  await userEvent.click(within(card).getByRole("button", { name: "Start session in api" }));
  expect(api.decideAskProposal).toHaveBeenCalledWith(PID, "start", true);
  const outcome = await screen.findByTestId("ask-session-outcome");
  expect(outcome).toHaveTextContent("Started");
  expect(outcome).toHaveTextContent("That is not the work done.");
  expect(within(outcome).getByTestId("ask-session-open")).toHaveAttribute("href", "/s/claude/abc-1");
  expect(screen.getByTestId("ask-session-bypass-used")).toHaveTextContent("On");
  expect(screen.queryByTestId("ask-session-start")).toBeNull();
});

test("an untouched switch starts with the default it shows", async () => {
  withConversation([sessionTurn(sessionCard())]);
  vi.mocked(api.decideAskProposal).mockResolvedValue({ proposal: settled({ state: "deciding" }) });
  await mountAt(<AskConsole configured />);
  await userEvent.click(await screen.findByTestId("ask-session-start"));
  expect(api.decideAskProposal).toHaveBeenCalledWith(PID, "start", false);
});

test("a changed default asks again: the card shows the new starting value and a notice", async () => {
  withConversation([sessionTurn(sessionCard())]);
  const fresh = sessionCard({ bypass_default: true, notice: "default_changed" });
  vi.mocked(api.decideAskProposal).mockRejectedValue(
    new ApiError(409, "your default for permission bypass changed", { proposal: fresh }),
  );
  await mountAt(<AskConsole configured />);
  const before = await screen.findByTestId("ask-session");
  expect(within(before).getByRole("switch")).not.toBeChecked();
  vi.mocked(api.askConversation).mockResolvedValue(conversation(CID, [sessionTurn(fresh)]));
  await userEvent.click(within(before).getByTestId("ask-session-start"));
  expect(await screen.findByTestId("ask-session-notice")).toHaveTextContent(
    "changed since this was proposed",
  );
  expect(screen.getByRole("switch")).toBeChecked();
  expect(screen.queryByTestId("ask-session-error")).toBeNull();
  expect(screen.getByTestId("ask-session-start")).toBeEnabled();
});

test("a refusal that did not change the card is said under it", async () => {
  withConversation([sessionTurn(sessionCard())]);
  vi.mocked(api.decideAskProposal).mockRejectedValue(
    new ApiError(409, "another session from this conversation is still starting"),
  );
  await mountAt(<AskConsole configured />);
  await userEvent.click(await screen.findByTestId("ask-session-start"));
  expect(await screen.findByTestId("ask-session-error")).toHaveTextContent(
    "still starting",
  );
  expect(screen.getByTestId("ask-session-start")).toBeEnabled();
});

test("Dismiss sends dismiss", async () => {
  withConversation([sessionTurn(sessionCard())]);
  vi.mocked(api.decideAskProposal).mockResolvedValue({ proposal: settled({ state: "dismissed" }) });
  await mountAt(<AskConsole configured />);
  await userEvent.click(await screen.findByTestId("ask-session-dismiss"));
  expect(api.decideAskProposal).toHaveBeenCalledWith(PID, "dismiss", false);
});

test.each([
  ["unconfirmed", "Not confirmed", "it may be running; check your sessions"],
  ["failed", "Failed", "nothing was started"],
  ["stale", "Out of date", "Nothing was started."],
  ["interrupted", "Interrupted", "It is not retried."],
  ["unknown", "Status unavailable", "updates on its own"],
  ["gone", "No longer tracked", "nothing will start"],
  ["dismissed", "Dismissed", "Nothing was started."],
] as const)("a %s session card says %s and offers no Start", async (state, word, detail) => {
  const reason =
    state === "unconfirmed"
      ? "no start evidence — it may be running; check your sessions. It is not retried."
      : state === "failed"
        ? "dtach exited 1 — nothing was started"
        : state === "stale"
          ? "the chosen project no longer exists"
          : state === "interrupted"
            ? "the server stopped while this was starting. It is not retried."
            : "";
  // An unconfirmed launch names the session it may have become, and still offers no way in as if
  // it had started.
  const session_key = state === "unconfirmed" ? "claude:abc-9" : "";
  withConversation([sessionTurn(settled({ state, reason, session_key }))]);
  await mountAt(<AskConsole configured />);
  const outcome = await screen.findByTestId("ask-session-outcome");
  expect(outcome).toHaveTextContent(word);
  expect(outcome).toHaveTextContent(detail);
  expect(screen.queryByTestId("ask-session-start")).toBeNull();
  expect(screen.queryByTestId("ask-session-open")).toBeNull();
});

test("a launch in flight is followed at the turn poll's pace, not the card timer's", async () => {
  withConversation([sessionTurn(settled({ state: "deciding", bypass: false }))]);
  await mountAt(<AskConsole configured cardPollMs={60_000} />);
  expect(await screen.findByTestId("ask-session-outcome")).toHaveTextContent("Starting");
  vi.mocked(api.askConversation).mockResolvedValue(
    conversation(CID, [sessionTurn(settled({ state: "started", session_key: "claude:abc-1" }))]),
  );
  await waitFor(
    () => expect(screen.getByTestId("ask-session-outcome")).toHaveTextContent("Started"),
    { timeout: 5000 },
  );
});

test("a settled session card stops re-reading", async () => {
  withConversation([sessionTurn(settled({ state: "dismissed" }))]);
  await mountAt(<AskConsole configured cardPollMs={20} />);
  await screen.findByTestId("ask-session-outcome");
  const reads = vi.mocked(api.askConversation).mock.calls.length;
  await new Promise((r) => setTimeout(r, 200));
  expect(vi.mocked(api.askConversation).mock.calls.length).toBe(reads);
});


test("a session card that could not be read keeps re-reading until it can", async () => {
  withConversation([sessionTurn(settled({ state: "unknown" }))]);
  await mountAt(<AskConsole configured cardPollMs={40} />);
  expect(await screen.findByTestId("ask-session-outcome")).toHaveTextContent("Status unavailable");
  vi.mocked(api.askConversation).mockResolvedValue(conversation(CID, [sessionTurn(sessionCard())]));
  expect(await screen.findByTestId("ask-session-start", {}, { timeout: 5000 })).toBeEnabled();
});

// Hermes on #1491, 5: the decision's own answer is applied to its card, whatever the re-read does.

test("an accepted Start shows Starting at once, even when the re-read fails", async () => {
  withConversation([sessionTurn(sessionCard())]);
  await mountAt(<AskConsole configured cardPollMs={60_000} />);
  const card = await screen.findByTestId("ask-session");
  vi.mocked(api.decideAskProposal).mockResolvedValue({
    proposal: settled({ state: "deciding", bypass: false }),
  });
  vi.mocked(api.askConversation).mockRejectedValue(new ApiError(503, "unavailable"));
  await userEvent.click(within(card).getByTestId("ask-session-start"));
  expect(await screen.findByTestId("ask-session-outcome")).toHaveTextContent("Starting");
  expect(screen.queryByTestId("ask-session-start")).toBeNull();
});

test("a changed default shows its notice and new value at once, even when the re-read fails", async () => {
  withConversation([sessionTurn(sessionCard())]);
  await mountAt(<AskConsole configured cardPollMs={60_000} />);
  const card = await screen.findByTestId("ask-session");
  vi.mocked(api.decideAskProposal).mockRejectedValue(
    new ApiError(409, "your default for permission bypass changed", {
      proposal: { ...sessionCard({ bypass_default: true, notice: "default_changed" }) },
    }),
  );
  vi.mocked(api.askConversation).mockRejectedValue(new ApiError(503, "unavailable"));
  await userEvent.click(within(card).getByTestId("ask-session-start"));
  expect(await screen.findByTestId("ask-session-notice")).toHaveTextContent(
    "changed since this was proposed",
  );
  expect(screen.getByRole("switch")).toBeChecked();
  expect(screen.queryByTestId("ask-session-error")).toBeNull();
});

test("a start refused because the project was being changed says so and can be tapped again", async () => {
  withConversation([sessionTurn(sessionCard({ notice: "busy" }))]);
  await mountAt(<AskConsole configured />);
  expect(await screen.findByTestId("ask-session-notice")).toHaveTextContent(
    "nothing started. Start it again.",
  );
  expect(screen.getByTestId("ask-session-start")).toBeEnabled();
});

// --- #1443 Phase 4: followed sessions' updates -------------------------------------------------

const FOLLOWED = "claude:abc-1";

function at(t: AskConversationTurn, asked: number): AskConversationTurn {
  return { ...t, asked_at: asked };
}

function update(over: Partial<AskUpdate> = {}): AskUpdate {
  return { key: FOLLOWED, kind: "finished", title: "api rebase", ts: 15, following: true, ...over };
}

function withUpdates(over: Partial<AskConversation>) {
  const turns = [
    at(turnOf("first", "t-1", { answer: "one" }), 10),
    at(turnOf("second", "t-2", { answer: "two" }), 20),
  ];
  vi.mocked(api.askConversations).mockResolvedValue({
    conversations: [{ id: CID, title: "first", created_at: 1, updated_at: 2 }],
    configured: true,
  });
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, turns),
    updates: [],
    following: [FOLLOWED],
    unread: false,
    ...over,
  });
}

test("updates never sit between turns: one row per session, its newest state (#1755)", async () => {
  const ENDED = "claude:abc-2";
  withUpdates({
    updates: [
      update({ ts: 5, kind: "needs_you" }),
      update({ ts: 15, kind: "finished" }),
      update({ ts: 17, kind: "finished" }),
      update({ key: ENDED, ts: 25, kind: "ended", title: "docs pass", following: false }),
    ],
  });
  await mountAt(<AskConsole configured />);
  const rows = await screen.findAllByTestId("ask-follow-row");
  // Two sessions, two rows; none inside a turn.
  expect(rows.map((r) => r.getAttribute("data-kind"))).toEqual(["ended", "finished"]);
  for (const t of screen.getAllByTestId("ask-turn")) {
    expect(within(t).queryByTestId("ask-follow-row")).toBeNull();
  }
  expect(rows[1]).toHaveTextContent("api rebase");
  expect(rows[1]).toHaveTextContent("Finished its turn");
  expect(rows[1]).toHaveTextContent("3 changes");
  expect(rows[1]).not.toHaveTextContent("Needs you");
  expect(rows[0]).toHaveTextContent("No longer followed");
  // A way in while it can still be opened; none once the follow has ended.
  expect(within(rows[1]).getByTestId("ask-follow-open")).toHaveAttribute("href", "/s/claude/abc-1");
  expect(within(rows[0]).queryByTestId("ask-follow-open")).toBeNull();
  expect(within(rows[0]).queryByTestId("ask-follow-unfollow")).toBeNull();
});

test("a new update changes its session's row in place, never adds one (#1755)", async () => {
  withUpdates({ updates: [update({ ts: 15 })] });
  await mountAt(<AskConsole configured cardPollMs={20} />);
  expect(await screen.findByTestId("ask-follow-row")).toHaveTextContent("Finished its turn");
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [update({ ts: 15 }), update({ ts: 16, kind: "needs_you" })],
    following: [FOLLOWED],
    unread: false,
  });
  await waitFor(() => expect(screen.getByTestId("ask-follow-row")).toHaveTextContent("Needs you"));
  expect(screen.getAllByTestId("ask-follow-row")).toHaveLength(1);
  expect(screen.getByTestId("ask-follow-row")).toHaveTextContent("2 changes");
});

test("Unfollow stops following and the session leaves the block", async () => {
  withUpdates({ updates: [update()] });
  await mountAt(<AskConsole configured />);
  const row = await screen.findByTestId("ask-follow-row");
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [update({ following: false })],
    following: [],
    unread: false,
  });
  await userEvent.click(within(row).getByRole("button", { name: "Unfollow api rebase" }));
  expect(api.askUnfollow).toHaveBeenCalledWith(CID, FOLLOWED);
  await waitFor(() => expect(screen.queryByTestId("ask-follow-row")).toBeNull());
});

test("a poll in flight when Unfollow lands never brings the row back (#1755)", async () => {
  withUpdates({ updates: [update()] });
  await mountAt(<AskConsole configured cardPollMs={20} />);
  const row = await screen.findByTestId("ask-follow-row");
  const fresh = {
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [update({ following: false })],
    following: [],
    unread: false,
  };
  let release!: () => void;
  const stale = new Promise<AskConversation>((resolve) => {
    release = () =>
      resolve({
        ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
        updates: [update()],
        following: [FOLLOWED],
        unread: false,
      });
  });
  vi.mocked(api.askConversation).mockResolvedValue(fresh);
  vi.mocked(api.askConversation).mockImplementationOnce(() => stale);
  const reads = vi.mocked(api.askConversation).mock.calls.length;
  await waitFor(() =>
    expect(vi.mocked(api.askConversation).mock.calls.length).toBeGreaterThan(reads),
  );
  await userEvent.click(within(row).getByRole("button", { name: "Unfollow api rebase" }));
  await waitFor(() => expect(screen.queryByTestId("ask-follow-row")).toBeNull());
  release();
  await new Promise((r) => setTimeout(r, 100));
  expect(screen.queryByTestId("ask-follow-row")).toBeNull();
});

test("an unreadable follow state keeps the last verified follows and keeps reading until it recovers", async () => {
  // Hermes on #1500: unknown is not "following nothing".
  withUpdates({ updates: [], follows: [{ key: FOLLOWED, title: "api rebase" }] });
  await mountAt(<AskConsole configured cardPollMs={20} />);
  await screen.findByTestId("ask-follow-row");
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [],
    following: [],
    follows: [],
    unread: false,
    follows_incomplete: true,
  });
  const reads = vi.mocked(api.askConversation).mock.calls.length;
  await waitFor(() =>
    expect(vi.mocked(api.askConversation).mock.calls.length).toBeGreaterThan(reads + 2),
  );
  expect(screen.getByTestId("ask-follow-row")).toHaveTextContent("api rebase");
  // The read recovers and carries the update that landed meanwhile.
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [update({ ts: 15 })],
    following: [FOLLOWED],
    follows: [{ key: FOLLOWED, title: "api rebase" }],
    unread: true,
  });
  await waitFor(
    () => expect(screen.getByTestId("ask-follow-row")).toHaveTextContent("Finished its turn"),
    { timeout: 5000 },
  );
});

test("an update still being delivered keeps the open screen reading until it lands", async () => {
  // Hermes on #1500: stored but not appended yet, with nothing followed and no live card.
  withUpdates({ updates: [], following: [], follows: [], unread: true, updates_pending: true });
  await mountAt(<AskConsole configured cardPollMs={20} />);
  await waitFor(() => expect(api.askConversation).toHaveBeenCalled());
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [update({ ts: 15, following: false, kind: "ended" })],
    following: [],
    follows: [],
    unread: true,
    updates_pending: false,
  });
  await waitFor(
    () => expect(screen.getByTestId("ask-follow-row")).toHaveTextContent("No longer followed"),
    { timeout: 5000 },
  );
});

test("a nudge Ask could not follow reads as Not following, with Open and no Unfollow (#1517)", async () => {
  withUpdates({
    updates: [
      update({ key: "claude:abc-3", ts: 15, kind: "not_followed", title: "docs pass", following: false }),
    ],
    following: [],
  });
  await mountAt(<AskConsole configured />);
  const row = await screen.findByTestId("ask-follow-row");
  expect(row).toHaveAttribute("data-kind", "not_followed");
  expect(row).toHaveTextContent("Not following");
  expect(row).toHaveTextContent("docs pass");
  expect(row).toHaveTextContent("Already following 10 sessions");
  expect(within(row).queryByRole("button", { name: /Unfollow/ })).toBeNull();
  expect(within(row).getByTestId("ask-follow-open")).toBeInTheDocument();
});

test("a conversation whose update budget is spent says it no longer follows sessions (#1517)", async () => {
  withUpdates({ updates: [update({ ts: 15 })], follows_exhausted: true });
  await mountAt(<AskConsole configured />);
  expect(await screen.findByTestId("ask-follow-exhausted")).toHaveTextContent(
    "This conversation no longer follows sessions. Start a new conversation to follow more.",
  );
});

test("a followed session with no update yet can be unfollowed (Hermes on #1500)", async () => {
  withUpdates({ updates: [], follows: [{ key: FOLLOWED, title: "api rebase" }] });
  await mountAt(<AskConsole configured />);
  const row = await screen.findByTestId("ask-follow-row");
  expect(row).toHaveAttribute("data-kind", "following");
  expect(row).toHaveTextContent("Following");
  expect(row).toHaveTextContent("No change yet");
  expect(row).toHaveTextContent("api rebase");
  expect(within(row).getByTestId("ask-follow-open")).toHaveAttribute("href", "/s/claude/abc-1");
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [],
    following: [],
    follows: [],
    unread: false,
  });
  await userEvent.click(within(row).getByRole("button", { name: "Unfollow api rebase" }));
  expect(api.askUnfollow).toHaveBeenCalledWith(CID, FOLLOWED);
  await waitFor(() => expect(screen.queryByTestId("ask-following")).toBeNull());
});

test("an unseen update is marked seen while the console is on screen, and says so to the icon", async () => {
  withUpdates({ updates: [update()], unread: true });
  const heard = vi.fn();
  window.addEventListener(ASK_SEEN_EVENT, heard);
  try {
    await mountAt(<AskConsole configured />);
    // Through the newest update it has SHOWN — never "now".
    await waitFor(() => expect(api.askSeen).toHaveBeenCalledWith(CID, 15));
    await waitFor(() => expect(heard).toHaveBeenCalled());
  } finally {
    window.removeEventListener(ASK_SEEN_EVENT, heard);
  }
});

test("an unfollowed session's update the block drops is acknowledged, so the dot can clear (#1755)", async () => {
  // The one update seen without being shown: the operator chose to stop hearing about it.
  withUpdates({
    updates: [
      update({ ts: 15 }),
      update({ key: "claude:abc-9", ts: 40, title: "old", following: false }),
    ],
    unread: true,
  });
  await mountAt(<AskConsole configured />);
  expect(await screen.findAllByTestId("ask-follow-row")).toHaveLength(1);
  await waitFor(() => expect(api.askSeen).toHaveBeenCalledWith(CID, 40));
});

test("…but not while the sidebar is closed", async () => {
  withUpdates({ updates: [update()], unread: true });
  await mountAt(<AskConsole configured visible={false} />);
  await screen.findByTestId("ask-follow-row");
  await new Promise((r) => setTimeout(r, 100));
  expect(api.askSeen).not.toHaveBeenCalled();
});

test("a conversation that follows sessions keeps reading, so new updates arrive", async () => {
  withUpdates({ updates: [] });
  await mountAt(<AskConsole configured cardPollMs={40} />);
  await screen.findAllByTestId("ask-turn");
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [update({ kind: "needs_you" })],
    following: [FOLLOWED],
    unread: false,
  });
  await waitFor(
    () => expect(screen.getByTestId("ask-follow-row")).toHaveTextContent("Needs you"),
    { timeout: 5000 },
  );
});

test("a conversation that follows nothing and has nothing live stops reading", async () => {
  withUpdates({ updates: [], following: [] });
  await mountAt(<AskConsole configured cardPollMs={20} />);
  await screen.findAllByTestId("ask-turn");
  const reads = vi.mocked(api.askConversation).mock.calls.length;
  await new Promise((r) => setTimeout(r, 200));
  expect(vi.mocked(api.askConversation).mock.calls.length).toBe(reads);
});

test("followRows: latest wins, needs-you then newest, notices kept, unfollowed dropped (#1755)", () => {
  const k = (n: number) => `claude:k-${n}`;
  const rows = followRows(
    [
      { key: k(1), title: "one" },
      { key: k(2), title: "" },
      { key: k(3), title: "three" },
    ],
    [
      update({ key: k(1), ts: 10, kind: "needs_you" }),
      update({ key: k(1), ts: 30, kind: "finished" }),
      update({ key: k(2), ts: 20, kind: "needs_you", title: "two" }),
      // Unfollowed by the operator: gone. Ended: kept as a notice, without Unfollow.
      update({ key: k(4), ts: 40, kind: "finished", following: false }),
      update({ key: k(5), ts: 35, kind: "ended", following: false }),
    ],
  );
  expect(rows.map((r) => [r.key, r.latest?.kind ?? null, r.changes, r.followed])).toEqual([
    [k(2), "needs_you", 1, true],
    [k(5), "ended", 1, false],
    [k(1), "finished", 2, true],
    [k(3), null, 0, true],
  ]);
  // A follow without a title takes the update's.
  expect(rows[0].title).toBe("two");
  // A tie on time: the later record wins.
  const tie = followRows(
    [{ key: k(1), title: "one" }],
    [update({ key: k(1), ts: 50, kind: "finished" }), update({ key: k(1), ts: 50, kind: "needs_you" })],
  );
  expect(tie[0].latest?.kind).toBe("needs_you");
  expect(tie[0].order).toBe(1);
});

test("followRows: a session followed AGAIN after a notice starts a new generation (#1755 review)", () => {
  const k = "claude:k-1";
  // Ended, then followed again by a new start: no change yet in this follow, never "ended".
  const again = followRows(
    [{ key: k, title: "one" }],
    [update({ key: k, ts: 10, kind: "finished" }), update({ key: k, ts: 20, kind: "ended", following: false })],
  );
  expect(again.map((r) => [r.latest?.kind ?? null, r.changes, r.followed])).toEqual([[null, 0, true]]);
  // Refused at the cap, then followed later and finished: this generation's one change.
  const later = followRows(
    [{ key: k, title: "one" }],
    [
      update({ key: k, ts: 10, kind: "not_followed", following: false }),
      update({ key: k, ts: 30, kind: "finished" }),
    ],
  );
  expect(later.map((r) => [r.latest?.kind ?? null, r.changes, r.followed])).toEqual([
    ["finished", 1, true],
  ]);
  // Not followed again: the notice stays the row.
  const gone = followRows([], [update({ key: k, ts: 20, kind: "ended", following: false })]);
  expect(gone.map((r) => [r.latest?.kind, r.followed])).toEqual([["ended", false]]);
});

test("followRows: follow → ended → follow again → Unfollow never brings the old notice back (#1755 review)", () => {
  const k = "claude:k-1";
  const history = [
    update({ key: k, ts: 10, kind: "finished" }),
    update({ key: k, ts: 20, kind: "ended", following: false }),
  ];
  // Followed again at 30: the ended notice is the earlier follow's.
  const again = followRows([{ key: k, title: "one" }], history, { [k]: 30 });
  expect(again.map((r) => [r.latest?.kind ?? null, r.changes, r.followed])).toEqual([[null, 0, true]]);
  // Unfollowed by the operator, then reloaded: the server still remembers since=30. Dropped.
  expect(followRows([], history, { [k]: 30 })).toEqual([]);
  // The same after a cap refusal, a later follow, and a manual Unfollow.
  const refused = [update({ key: k, ts: 10, kind: "not_followed", following: false })];
  expect(followRows([], refused, { [k]: 30 })).toEqual([]);
  // A notice from the CURRENT follow (newer than since) stays.
  const ended = followRows([], [update({ key: k, ts: 40, kind: "ended", following: false })], { [k]: 30 });
  expect(ended.map((r) => r.latest?.kind)).toEqual(["ended"]);
});

test("a re-followed session unfollowed and reloaded leaves no row behind (#1755 review)", async () => {
  withUpdates({
    updates: [update({ ts: 15, kind: "ended", following: false })],
    following: [],
    follows: [],
    follow_since: { [FOLLOWED]: 30 },
  });
  await mountAt(<AskConsole configured />);
  await screen.findAllByTestId("ask-turn");
  expect(screen.queryByTestId("ask-follow-row")).toBeNull();
  expect(screen.queryByText("No longer followed")).toBeNull();
});

test("a session followed again after it ended reads as Following, with Open and Unfollow (#1755 review)", async () => {
  withUpdates({
    updates: [update({ ts: 15, kind: "ended", following: false })],
    follows: [{ key: FOLLOWED, title: "api rebase" }],
  });
  await mountAt(<AskConsole configured />);
  const row = await screen.findByTestId("ask-follow-row");
  expect(row).toHaveAttribute("data-kind", "following");
  expect(row).not.toHaveTextContent("No longer followed");
  expect(within(row).getByTestId("ask-follow-open")).toBeInTheDocument();
  expect(within(row).getByRole("button", { name: "Unfollow api rebase" })).toBeInTheDocument();
});

test("the block announces only the newest change, once, never the whole re-sorted list (#1755)", async () => {
  withUpdates({ updates: [update({ ts: 15 })] });
  await mountAt(<AskConsole configured cardPollMs={20} />);
  const status = await screen.findByTestId("ask-follow-status");
  expect(status).toHaveAttribute("role", "status");
  expect(screen.getByTestId("ask-following")).not.toHaveAttribute("aria-live");
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [update({ ts: 15 }), update({ ts: 16, kind: "needs_you" })],
    following: [FOLLOWED],
    unread: false,
  });
  await waitFor(() => expect(status).toHaveTextContent("api rebase: Needs you, 2 changes"));
  // A repeat of the same word at the same time is still a change: the line moves once more.
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [
      update({ ts: 15 }),
      update({ ts: 16, kind: "needs_you" }),
      update({ ts: 16, kind: "needs_you" }),
    ],
    following: [FOLLOWED],
    unread: false,
  });
  await waitFor(() => expect(status).toHaveTextContent("api rebase: Needs you, 3 changes"));
  expect(screen.getAllByTestId("ask-follow-row")).toHaveLength(1);
});

test("the status line names the newest ARRIVAL, not the largest time, across sessions (#1755)", async () => {
  // Two sessions finished at the same second: the one that arrived last is announced, though the
  // other sorts first.
  withUpdates({
    updates: [
      update({ key: "claude:abc-2", ts: 20, kind: "finished", title: "docs pass" }),
      update({ ts: 20, kind: "finished" }),
    ],
    following: ["claude:abc-2", FOLLOWED],
  });
  await mountAt(<AskConsole configured />);
  const rows = await screen.findAllByTestId("ask-follow-row");
  expect(rows[0]).toHaveTextContent("docs pass");
  expect(screen.getByTestId("ask-follow-status")).toHaveTextContent("api rebase: Finished its turn");
});

test("the switcher marks a conversation with an unseen update, in words as well as the dot", async () => {
  withUpdates({});
  vi.mocked(api.askConversations).mockResolvedValue({
    conversations: [
      { id: CID, title: "first", created_at: 1, updated_at: 3 },
      { id: OTHER, title: "other", created_at: 1, updated_at: 2, unread: true },
    ],
    configured: true,
  });
  await mountAt(<AskConsole configured />);
  await screen.findAllByTestId("ask-turn");
  await userEvent.click(screen.getByTestId("ask-switcher"));
  const items = await screen.findAllByTestId("ask-switcher-item");
  expect(within(items[0]).queryByTestId("ask-switcher-unread")).toBeNull();
  expect(within(items[1]).getByTestId("ask-switcher-unread")).toHaveTextContent("New updates");
});

// --- Hermes round 2 on #1500 ----------------------------------------------------------------------

test("a session started elsewhere is read until the follow it earns arrives", async () => {
  // Another tab started it: this console's read shows it Started, not followed YET (the
  // follower's next pass is up to a minute away), and nothing else is live — it keeps reading.
  const turn = sessionTurn(sessionCard());
  withConversation([turn]);
  await mountAt(<AskConsole configured cardPollMs={40} />);
  await screen.findByTestId("ask-session-start");
  const started = {
    ...conversation(CID, [sessionTurn(settled({ state: "started", session_key: FOLLOWED }))]),
    updates: [],
    following: [],
    unread: false,
  };
  vi.mocked(api.askConversation).mockResolvedValue(started);
  expect(await screen.findByTestId("ask-session-outcome", {}, { timeout: 5000 })).toHaveTextContent(
    "Started",
  );
  await new Promise((r) => setTimeout(r, 150)); // several empty reads
  vi.mocked(api.askConversation).mockResolvedValue({
    ...started,
    updates: [update({ ts: 50, kind: "finished" })],
    following: [FOLLOWED],
  });
  await waitFor(
    () => expect(screen.getByTestId("ask-follow-row")).toHaveTextContent("Finished its turn"),
    { timeout: 5000 },
  );
});

test.each(["delivered", "indeterminate"])(
  "a nudge %s elsewhere is read until the follow it earns arrives (Hermes on #1517)",
  async (outcome) => {
    // The nudge was approved on another surface (Needs you, the bell): this console's read shows
    // it settled, not followed YET — the follower admits it at its next pass, up to a minute away —
    // and nothing else is live. It keeps reading, so the follow and its update reach this screen.
    const nudgeTurn = (card: AskNudgeCard) => ({
      ...turnOf("tell it to rebase", "t-n", { answer: "Proposed." }),
      proposals: [card],
    });
    withConversation([nudgeTurn(nudge())]);
    await mountAt(<AskConsole configured cardPollMs={40} />);
    await screen.findByTestId("ask-nudge-send");
    const settledRead = {
      ...conversation(CID, [
        nudgeTurn(
          nudge({
            state: outcome,
            projection: "settled",
            can_approve: false,
            can_reject: false,
            key: FOLLOWED,
          }),
        ),
      ]),
      updates: [],
      following: [],
      unread: false,
    };
    vi.mocked(api.askConversation).mockResolvedValue(settledRead);
    await waitFor(() => expect(screen.queryByTestId("ask-nudge-send")).toBeNull(), {
      timeout: 5000,
    });
    await new Promise((r) => setTimeout(r, 150)); // several reads before the follower's pass
    vi.mocked(api.askConversation).mockResolvedValue({
      ...settledRead,
      updates: [update({ ts: 50, kind: "finished" })],
      following: [FOLLOWED],
    });
    await waitFor(
      () => expect(screen.getByTestId("ask-follow-row")).toHaveTextContent("Finished its turn"),
      { timeout: 5000 },
    );
  },
);

test("the read after a start is bounded: it stops once the window ends", async () => {
  withConversation([sessionTurn(settled({ state: "started", session_key: FOLLOWED }))]);
  await mountAt(<AskConsole configured cardPollMs={20} reconcileMs={120} />);
  await screen.findByTestId("ask-session-outcome");
  await new Promise((r) => setTimeout(r, 300)); // past the window
  const reads = vi.mocked(api.askConversation).mock.calls.length;
  await new Promise((r) => setTimeout(r, 200));
  expect(vi.mocked(api.askConversation).mock.calls.length).toBe(reads);
});

test("an older seen answer arriving late never brings 'unread' back", async () => {
  withUpdates({ updates: [update({ ts: 15 })], unread: true });
  const first = deferred<{ unread: boolean }>();
  vi.mocked(api.askSeen)
    .mockReturnValueOnce(first.promise)
    .mockResolvedValue({ unread: false });
  await mountAt(<AskConsole configured cardPollMs={40} />);
  await waitFor(() => expect(api.askSeen).toHaveBeenCalledTimes(1));
  // A newer update arrives and is shown; its mark answers first…
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [update({ ts: 15 }), update({ ts: 25, kind: "needs_you" })],
    following: [FOLLOWED],
    unread: true,
  });
  await waitFor(() => expect(api.askSeen).toHaveBeenCalledWith(CID, 25));
  vi.mocked(api.askConversation).mockResolvedValue({
    ...conversation(CID, [at(turnOf("first", "t-1", { answer: "one" }), 10)]),
    updates: [update({ ts: 15 }), update({ ts: 25, kind: "needs_you" })],
    following: [FOLLOWED],
    unread: false,
  });
  // A poll already in flight may still answer "unread" and rightly mark it seen again. Let two
  // reads land after the swap before taking the baseline, so only the stale answer is measured.
  const reads = vi.mocked(api.askConversation).mock.calls.length;
  await waitFor(() =>
    expect(vi.mocked(api.askConversation).mock.calls.length).toBeGreaterThan(reads + 1),
  );
  await act(async () => {});
  const calls = vi.mocked(api.askSeen).mock.calls.length;
  // …then the OLD mark's stale "still unread" lands. It must change nothing.
  await act(async () => first.resolve({ unread: true }));
  await new Promise((r) => setTimeout(r, 150));
  expect(vi.mocked(api.askSeen).mock.calls.length).toBe(calls);
});

// ---- HQ contacts (#1525) -------------------------------------------------------------------------

function withContact(choice: string, ui: React.ReactNode) {
  return (
    <ConfigCtx.Provider value={{ hq_contact: choice } as AppConfig}>
      <MemoryRouter>{ui}</MemoryRouter>
    </ConfigCtx.Provider>
  );
}

function answerEchoingContact() {
  vi.mocked(api.askMessageStream).mockImplementation(async (_id, body) => ({
    ...turnOf(body.text, body.turnId, { answer: `answer to ${body.text}` }),
    contact: body.contact ?? "off",
  }));
}

test("the head shows the selected contact: portrait, HQ and the name over the picker, + and ×", () => {
  render(withContact("southamerica-woman", <AskConsole configured onClose={() => undefined} />));
  const head = screen.getByTestId("ask-head");
  expect(head).toHaveAttribute("data-contact", "southamerica-woman");
  expect(within(head).getByTestId("hq-head-portrait")).toHaveAttribute("aria-hidden", "true");
  expect(within(head).getByTestId("hq-head-name")).toHaveTextContent("General Valentina Cruz");
  expect(within(head).getAllByRole("button")).toEqual([
    within(head).getByTestId("ask-switcher"),
    within(head).getByRole("button", { name: "Delete this conversation" }),
    within(head).getByRole("button", { name: "New conversation" }),
    within(head).getByRole("button", { name: "Close HQ" }),
  ]);
});

test("Off: the head is one row, with no portrait and no media at all", () => {
  const { container } = render(withContact("off", <AskConsole configured onClose={() => undefined} />));
  const head = screen.getByTestId("ask-head");
  expect(head).toHaveAttribute("data-contact", "off");
  expect(within(head).queryByTestId("hq-head-portrait")).toBeNull();
  expect(within(head).queryByTestId("hq-head-name")).toBeNull();
  expect(container.querySelector("video, img")).toBeNull();
});

test("each answer is labelled by ITS turn's contact; switching relabels new answers only", async () => {
  answerEchoingContact();
  vi.mocked(api.askNewConversation).mockResolvedValue(conversation(CID, []));
  const ui = (choice: string) => withContact(choice, <AskConsole configured />);
  const { rerender } = render(ui("southamerica-woman"));
  await userEvent.type(screen.getByRole("textbox"), "status?{Enter}");
  expect(await screen.findByRole("article", { name: "Answer from General Valentina Cruz" })).toBeInTheDocument();
  expect(vi.mocked(api.askMessageStream).mock.calls[0][1]).toMatchObject({ contact: "southamerica-woman" });

  rerender(ui("romania-woman"));
  await userEvent.type(screen.getByRole("textbox"), "and now?{Enter}");
  expect(await screen.findByRole("article", { name: "Answer from General Ileana Lupu" })).toBeInTheDocument();
  expect(vi.mocked(api.askMessageStream).mock.calls[1][1]).toMatchObject({ contact: "romania-woman" });
  // The first answer keeps its speaker.
  expect(screen.getByRole("article", { name: "Answer from General Valentina Cruz" })).toBeInTheDocument();
  expect(screen.getAllByTestId("ask-speaker").map((h) => h.textContent)).toEqual([
    "General Valentina Cruz",
    "General Ileana Lupu",
  ]);
});

test("Off, an unknown contact and a turn from before HQ all answer as plain HQ", async () => {
  twoConversations();
  vi.mocked(api.askConversation).mockResolvedValue(
    conversation(CID, [
      turnOf("old", "t1", { answer: "before HQ" }),
      { ...turnOf("gone", "t2", { answer: "removed contact" }), contact: "general-nobody" },
      { ...turnOf("off", "t3", { answer: "off" }), contact: "off" },
    ]),
  );
  render(withContact("us-man", <AskConsole configured />));
  await pick(0);
  await screen.findByText("removed contact");
  expect(screen.getAllByRole("article", { name: "Answer from HQ" })).toHaveLength(3);
});

// --- #1559: HQ operations, confirmed on a card ------------------------------------------------

function opCard(over: Partial<AskOperationCard> = {}): AskOperationCard {
  return {
    action_id: PID,
    kind: "operation",
    op: "session.archive",
    risk: "reversible",
    label: "Archive",
    verb: "Archive",
    title: "docs-site: rewrite the install guide",
    text: "Stops its agent and moves it to the archive.",
    needs: [],
    state: "pending",
    summary: "",
    reason: "",
    can_confirm: true,
    can_dismiss: true,
    ...over,
  };
}

function opTurn(card: AskOperationCard): AskConversationTurn {
  return {
    ...turnOf("archive the docs session", "t-o", { answer: "Proposed." }),
    proposals: [card],
  };
}

test("an operation card shows the server's wording and confirms with nothing but the decision", async () => {
  withConversation([opTurn(opCard())]);
  vi.mocked(api.decideAskOperation).mockImplementation(async () => {
    // The server now holds it as running; the console's re-read says so too.
    vi.mocked(api.askConversation).mockResolvedValue(
      conversation(CID, [
        opTurn(
          opCard({ state: "deciding", can_confirm: false, can_dismiss: false }),
        ),
      ]),
    );
    return {
      proposal: {
        state: "deciding",
        summary: "",
        reason: "",
        can_confirm: false,
        can_dismiss: false,
      },
    };
  });
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-operation");
  expect(card).toHaveTextContent("Archive");
  expect(card).toHaveTextContent("docs-site: rewrite the install guide");
  expect(within(card).getByTestId("ask-operation-text")).toHaveTextContent(
    "Stops its agent and moves it to the archive.",
  );
  expect(card).toHaveTextContent("Reversible");
  await userEvent.click(within(card).getByTestId("ask-operation-confirm"));
  expect(api.decideAskOperation).toHaveBeenCalledWith(
    PID,
    "confirm",
    undefined,
  );
  expect(
    await within(card).findByTestId("ask-operation-outcome"),
  ).toHaveTextContent("Running");
});

// --- #1786: secret cards ----------------------------------------------------------------------

test("a store-secret card takes the value on the card, sends it once and keeps none of it", async () => {
  withConversation([
    opTurn(
      opCard({
        op: "secret.create",
        label: "Store secret",
        verb: "Store",
        title: "deploy_token",
        text: "Stores a new secret “deploy_token” in the Library.",
        needs: ["value"],
      }),
    ),
  ]);
  vi.mocked(api.decideAskOperation).mockResolvedValue({
    proposal: { state: "deciding", summary: "", reason: "", can_confirm: false, can_dismiss: false },
  });
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-operation");
  expect(card).toHaveTextContent("Secret");
  const confirm = within(card).getByTestId("ask-operation-confirm");
  expect(confirm).toBeDisabled();
  const field = within(card).getByLabelText("Value");
  expect(field).toHaveAttribute("type", "password");
  await userEvent.type(field, "card-typed-value-1786");
  expect(confirm).toBeEnabled();
  await userEvent.click(confirm);
  expect(api.decideAskOperation).toHaveBeenCalledWith(PID, "confirm", {
    value: "card-typed-value-1786",
  });
  // Cleared at once: whatever the card shows next, the typed value is no longer held in it.
  const after = within(card).queryByLabelText("Value");
  if (after) expect(after).toHaveValue("");
});

test("turning a secret's agent access on is stamped Widens access", async () => {
  withConversation([
    opTurn(
      opCard({
        op: "secret.access_on",
        risk: "external",
        label: "Turn agent access on",
        verb: "Turn on",
        title: "asc_api_key",
        text: "Lets agents in Atlas iOS request a private copy of “asc_api_key”.",
      }),
    ),
  ]);
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-operation");
  expect(card).toHaveTextContent("Widens access");
  expect(card).not.toHaveTextContent("External");
  expect(within(card).queryByLabelText("Value")).toBeNull();
  expect(within(card).getByTestId("ask-operation-confirm")).toBeEnabled();
});

test("a destructive card says so and uses the danger confirm", async () => {
  withConversation([
    opTurn(
      opCard({
        op: "project.delete",
        risk: "destructive",
        label: "Delete project",
        verb: "Delete project",
      }),
    ),
  ]);
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-operation");
  expect(card).toHaveTextContent("Can't be undone");
  expect(within(card).getByTestId("ask-operation-danger")).toHaveTextContent(
    "can't be undone",
  );
  expect(
    within(card).getByRole("button", { name: "Delete project" }),
  ).toBeEnabled();
});

test("a card that needs a folder can't be confirmed until the operator picks one; the mkdir note is shown", async () => {
  withConversation([
    opTurn(
      opCard({
        op: "project.create",
        label: "Create project",
        verb: "Create project",
        needs: ["folder"],
        title: "",
      }),
    ),
  ]);
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-operation");
  expect(within(card).getByTestId("ask-operation-confirm")).toBeDisabled();
  expect(
    within(card).getByTestId("ask-operation-mkdir-note"),
  ).toHaveTextContent("creates the folder now");
  expect(within(card).getByTestId("ask-operation-pick")).toBeEnabled();
});

test("a project-folder choice is a radio group from the server's list; the confirm carries the pick", async () => {
  withConversation([
    opTurn(
      opCard({
        op: "project.remove_folder",
        label: "Release a folder",
        verb: "Release folder",
        needs: ["folder"],
        choices: { folder: ["/home/u/web", "/home/u/web-old"] },
      }),
    ),
  ]);
  vi.mocked(api.decideAskOperation).mockResolvedValue({
    proposal: {
      state: "deciding",
      summary: "",
      reason: "",
      can_confirm: false,
      can_dismiss: false,
    },
  });
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-operation");
  const confirm = within(card).getByTestId("ask-operation-confirm");
  expect(confirm).toBeDisabled();
  await userEvent.click(
    within(card).getByRole("radio", { name: "/home/u/web-old" }),
  );
  expect(
    within(card).getByRole("radio", { name: "/home/u/web-old" }),
  ).toHaveAttribute("aria-checked", "true");
  await userEvent.click(confirm);
  expect(api.decideAskOperation).toHaveBeenCalledWith(PID, "confirm", {
    folder: "/home/u/web-old",
  });
});

test.each([
  ["done", "Done", "Renamed."],
  ["partly_done", "Partly done", "12 archived; 2 could not be."],
  ["failed", "Failed", "Not archived: refused."],
  [
    "stale",
    "Out of date",
    "it was renamed since this was proposed; nothing ran",
  ],
  ["interrupted", "Interrupted", "It may have run"],
  ["dismissed", "Dismissed", "Nothing ran."],
  ["unconfirmed", "Ran, unconfirmed", "HQ can't say what changed."],
] as const)(
  "a %s operation reads %s with the route's own words and offers nothing",
  async (state, word, detail) => {
    const reason =
      state === "stale"
        ? detail
        : state === "interrupted"
          ? "BattleLab stopped while this ran. It may have run; check the target."
          : "";
    const summary = ["done", "partly_done", "failed", "unconfirmed"].includes(state)
      ? detail
      : "";
    withConversation([
      opTurn(
        opCard({
          state,
          summary,
          reason,
          can_confirm: false,
          can_dismiss: false,
        }),
      ),
    ]);
    await mountAt(<AskConsole configured />);
    const outcome = await screen.findByTestId("ask-operation-outcome");
    expect(outcome).toHaveTextContent(word);
    expect(outcome).toHaveTextContent(detail);
    expect(screen.queryByTestId("ask-operation-confirm")).toBeNull();
  },
);

test("a refused confirm that left the card pending is said under it", async () => {
  withConversation([opTurn(opCard())]);
  vi.mocked(api.decideAskOperation).mockRejectedValue(
    new ApiError(422, "choose a folder on the card first"),
  );
  await mountAt(<AskConsole configured />);
  const card = await screen.findByTestId("ask-operation");
  await userEvent.click(within(card).getByTestId("ask-operation-confirm"));
  expect(
    await within(card).findByTestId("ask-operation-error"),
  ).toHaveTextContent("choose a folder");
});

test("a picked project folder the refreshed card no longer offers is dropped, never confirmed", async () => {
  const card = (folders: string[]) =>
    opCard({
      op: "project.default_folder",
      label: "Default launch folder",
      verb: "Set default folder",
      needs: ["folder"],
      choices: { folder: folders },
    });
  const onConfirm = vi.fn();
  const view = (folders: string[]) => (
    <OperationCard
      p={card(folders)}
      deciding={false}
      error={null}
      onConfirm={onConfirm}
      onDismiss={() => {}}
    />
  );
  const { rerender } = render(view(["/srv/a", "/srv/x"]));
  await userEvent.click(screen.getByRole("radio", { name: "/srv/a" }));
  expect(screen.getByTestId("ask-operation-confirm")).toBeEnabled();
  rerender(view(["/srv/b", "/srv/x"])); // A was replaced by B meanwhile
  expect(screen.getByTestId("ask-operation-folder")).toHaveTextContent("Choose below");
  expect(screen.getByTestId("ask-operation-confirm")).toBeDisabled();
  for (const r of screen.getAllByRole("radio")) {
    expect(r).toHaveAttribute("aria-checked", "false");
  }
  expect(onConfirm).not.toHaveBeenCalled();
});

test("the folder radio group is one Tab stop and arrow keys move and select (#1571 review)", async () => {
  const onConfirm = vi.fn();
  render(
    <OperationCard
      p={opCard({
        op: "project.remove_folder",
        label: "Release a folder",
        verb: "Release folder",
        needs: ["folder"],
        choices: { folder: ["/srv/a", "/srv/b", "/srv/c"] },
      })}
      deciding={false}
      error={null}
      onConfirm={onConfirm}
      onDismiss={() => {}}
    />,
  );
  const radios = screen.getAllByRole("radio");
  expect(radios.map((r) => r.tabIndex)).toEqual([0, -1, -1]);
  radios[0].focus();
  await userEvent.keyboard("{ArrowDown}");
  expect(screen.getByRole("radio", { name: "/srv/a" })).toHaveAttribute("aria-checked", "true");
  await userEvent.keyboard("{ArrowDown}");
  const b = screen.getByRole("radio", { name: "/srv/b" });
  expect(b).toHaveAttribute("aria-checked", "true");
  expect(b).toHaveFocus();
  expect(screen.getAllByRole("radio").map((r) => r.tabIndex)).toEqual([-1, 0, -1]);
  await userEvent.keyboard("{ArrowUp}{ArrowUp}");
  expect(screen.getByRole("radio", { name: "/srv/c" })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("radio", { name: "/srv/c" })).toHaveFocus();
});
