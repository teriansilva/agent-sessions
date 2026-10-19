/** ASK (#878; a page since #1058, the right-hand sidebar since #1294, durable since #1443) — the
 *  box's behaviour and the conversation's lifecycle.
 *
 *  Since #1443 the conversation is the server's (`/api/ask/*`): the console reopens the latest one,
 *  creates a conversation on its first question, streams a turn the server owns, and — when a
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
  AskConversation,
  AskConversationTurn,
  AskStoredMatch,
  AskStoredMissionMatch,
  AskTurnEvent,
} from "../../types/api";

import { AskConsole } from "./AskConsole";
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
      askMessageStream: vi.fn(),
    },
  };
});

const CID = "ask_" + "1".repeat(32);
const OTHER = "ask_" + "2".repeat(32);

/** The console needs a router: a match row renders a `<Link>` into the session. */
function mount(ui: React.ReactNode) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
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
  vi.mocked(api.askMessageStream).mockImplementation(async (_id, body) =>
    turnOf(body.text, body.turnId, result),
  );
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
  vi.mocked(api.askConversations).mockResolvedValue({ conversations: [], configured: true });
  vi.mocked(api.askNewConversation).mockResolvedValue(conversation(CID));
  vi.mocked(api.askConversation).mockResolvedValue(conversation(CID));
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

test("leaving never cancels the turn: coming back reopens the latest conversation, settled (#1443)", async () => {
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

  // Coming back reads the server: the latest conversation, with the settled answer.
  vi.mocked(api.askConversations).mockResolvedValue({
    conversations: [{ id: CID, title: "for this visit", created_at: 1, updated_at: 2 }],
    configured: true,
  });
  vi.mocked(api.askConversation).mockResolvedValue(conversation(CID, [done]));
  await userEvent.click(screen.getByRole("button", { name: "return" }));
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

test("the head is the switcher, New conversation and the sidebar's Close — nothing else (#1294, #1443)", async () => {
  answerWith({ answer: "ok" });
  const onClose = vi.fn();
  mount(<AskConsole configured onClose={onClose} />);
  const head = screen.getByTestId("ask-head");
  const switcher = within(head).getByTestId("ask-switcher");
  const fresh = within(head).getByRole("button", { name: "New conversation" });
  const close = within(head).getByRole("button", { name: "Close Ask" });
  expect(within(head).getAllByRole("button")).toEqual([switcher, fresh, close]);
  // No way "back": Ask is not a page any more.
  expect(within(head).queryAllByRole("link")).toHaveLength(0);
  await userEvent.click(close);
  expect(onClose).toHaveBeenCalledTimes(1);
  // Nothing to start over from yet.
  expect(fresh).toBeDisabled();

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

test("opening Ask reopens the latest conversation", async () => {
  twoConversations();
  mount(<AskConsole configured />);
  expect(await screen.findByText(`answer for ${CID}`)).toBeInTheDocument();
  expect(api.askConversation).toHaveBeenCalledWith(CID);
  expect(screen.getByTestId("ask-switcher")).toHaveTextContent("the latest");
});

test("the switcher lists latest first and opens the one picked — by keyboard", async () => {
  twoConversations();
  mount(<AskConsole configured />);
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
  mount(<AskConsole configured />);
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
    mount(<AskConsole configured />);
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

test("a question asked before the first selection settles waits for it and is never swept away", async () => {
  const list = deferred<{
    conversations: { id: string; title: string; created_at: number; updated_at: number }[];
    configured: boolean;
  }>();
  vi.mocked(api.askConversations).mockReturnValueOnce(list.promise as never);
  const earlier = turnOf("earlier", "t0", { answer: "the earlier answer" });
  vi.mocked(api.askConversation).mockResolvedValue(conversation(CID, [earlier]));
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
  // …then it goes to the conversation that opened, beside that conversation's earlier turns.
  expect(await screen.findByText("kept")).toBeInTheDocument();
  expect(screen.getByText("the earlier answer")).toBeInTheDocument();
  expect(vi.mocked(api.askMessageStream).mock.calls[0][0]).toBe(CID);
  expect(api.askNewConversation).not.toHaveBeenCalled();
  expect(screen.getAllByTestId("ask-turn")).toHaveLength(2);
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
  mount(<AskConsole configured />);
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

test("a question sent while the conversation's read is still out keeps its answer and shows the rest", async () => {
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
  mount(<AskConsole configured />);
  await waitFor(() => expect(api.askConversation).toHaveBeenCalledWith(CID));
  await userEvent.click(screen.getByTestId("ask-switcher"));
  await userEvent.click(screen.getAllByTestId("ask-switcher-item")[1]);
  await userEvent.type(screen.getByTestId("composer-input"), "into b{Enter}");
  expect(await screen.findByText("new answer")).toBeInTheDocument();
  // The read started before the send lands now, with B as it was before: it must not wipe the
  // new answer…
  await act(async () => slowB.resolve(conversation(OTHER, [earlier])));
  expect(screen.getByText("new answer")).toBeInTheDocument();
  // …and B's earlier turns are shown all the same (re-read after the send settled).
  expect(await screen.findByText("an earlier answer")).toBeInTheDocument();
  expect(vi.mocked(api.askMessageStream).mock.calls[0][0]).toBe(OTHER);
});

test("a refresh that reorders the open switcher: Enter opens the focused conversation", async () => {
  vi.mocked(api.askConversations)
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
  mount(<AskConsole configured />);
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
