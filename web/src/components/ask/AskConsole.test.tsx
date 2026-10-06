/** ASK (#878; a page since #1058, the right-hand sidebar since #1294) — the box's behaviour, plus the lifecycle rules that only
 *  exist because the answers are transient.
 *
 *  Five of these descend from `Composer.test.tsx`, which descended from `Pulse.test.tsx`'s Ask cases
 *  (#522): the answer renders, a 409 surfaces the server's own detail rather than a generic error, a
 *  follow-up replays the prior turns as history, an unconfigured endpoint disables the control and
 *  makes no call, and the page says the answers are not kept.
 *
 *  **What changed with the move, and what it means for these tests.** The composer's discard rule
 *  needed a `visit()` token compared at resolution time, because switching missions or flipping
 *  Active → Archived did not necessarily unmount the box — an answer could arrive into a surface
 *  that was still on screen but was no longer the one that asked. On a route that cannot happen:
 *  leaving unmounts the page and the turns are its own state. So the "discarded, not filed" case
 *  below asserts the OBSERVABLE consequence (coming back finds nothing, and the late resolution
 *  neither throws nor resurrects a turn) and says openly that the unmount is what makes it hold —
 *  rather than pretending a ref is doing work that React's own lifecycle does.
 *
 *  The ref DOES do one job, and the last test is the one that is red without it: under StrictMode an
 *  effect is cleaned up and re-run on the same fiber, and a `useRef(true)` initialises once — so a
 *  cleanup-only liveness flag stays false for the rest of the component's life and drops every
 *  answer in development. That is a real regression with a real red test, which is the only kind
 *  worth writing here.
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
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { api, ApiError } from "../../lib/api";
import type { PulseAskEvent, PulseAskResult } from "../../types/api";

import { AskConsole } from "./AskConsole";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: { pulseAskStream: vi.fn() },
  };
});

/** The console needs a router: a match row renders a `<Link>` into the session. */
function mount(ui: React.ReactNode) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

/** The page's own mount/unmount, driven from a control — the route transition these tests are
 *  about, without pulling the whole router in. */
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

/** The server's final answer, as the stream delivers it (#1171). */
function answerWith(result: PulseAskResult) {
  vi.mocked(api.pulseAskStream).mockImplementation(async (_q, _h, onEvent) => {
    onEvent({ type: "answer", final: true, ...result });
  });
}

beforeEach(() => {
  vi.mocked(api.pulseAskStream).mockReset();
});

test("a question renders its answer in the thread (#522)", async () => {
  answerWith({
    answer: "You merged it on Tuesday.",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  mount(<AskConsole configured />);
  await userEvent.type(
    screen.getByTestId("composer-input"),
    "when did I merge it",
  );
  await userEvent.click(screen.getByTestId("composer-send"));
  const turns = await screen.findByTestId("ask-turns");
  expect(
    within(turns).getByText("You merged it on Tuesday."),
  ).toBeInTheDocument();
});

test("a matched session is named, explained and reachable (#522)", async () => {
  // An answer that names a session the operator cannot open is half an answer — the match row
  // carries the reason it matched AND the way in.
  answerWith({
    answer: "Two sessions touched it.",
    matches: [
      { id: "claude:abc-123", title: "fix flaky upload retry", why: "uploads.py" },
    ],
    stage: "catalog",
    configured: true,
  });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "upload retry");
  await userEvent.click(screen.getByTestId("composer-send"));
  const row = await screen.findByTestId("ask-match");
  expect(within(row).getByText("uploads.py")).toBeInTheDocument();
  expect(
    within(row).getByRole("link", { name: "Jump into fix flaky upload retry" }),
  ).toHaveAttribute("href", "/s/claude/abc-123");
});

test("a matched mission is named, explained and opens the mission (#1069)", async () => {
  const mid = "msn_" + "a".repeat(32);
  answerWith({
    answer: "The reconnect mission ran it.",
    matches: [
      { id: "claude:abc-123", title: "ws backoff", why: "did the work" },
    ] as never,
    mission_matches: [
      {
        id: mid,
        title: "Stabilise terminal reconnects",
        state: "done",
        project_id: "",
        why: "the instruction names it",
      },
    ],
    stage: "content",
    configured: true,
  });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "reconnect");
  await userEvent.click(screen.getByTestId("composer-send"));
  const row = await screen.findByTestId("ask-mission-match");
  expect(within(row).getByText("the instruction names it")).toBeInTheDocument();
  expect(
    within(row).getByRole("link", {
      name: "Open mission Stabilise terminal reconnects",
    }),
  ).toHaveAttribute("href", `/mission?m=${mid}`);
  // Both groups are labelled, missions first, and the session keeps its own way in.
  const turn = screen.getByTestId("ask-turn");
  const heads = Array.from(turn.querySelectorAll("div"))
    .map((d) => d.textContent)
    .filter((t) => t === "Missions" || t === "Sessions");
  expect(heads).toEqual(["Missions", "Sessions"]);
  expect(
    within(turn).getByRole("link", { name: "Jump into ws backoff" }),
  ).toHaveAttribute("href", "/s/claude/abc-123");
});

test("a sessions-only answer has no group labels, as before #1069", async () => {
  answerWith({
    answer: "One session.",
    matches: [{ id: "claude:abc-123", title: "t", why: "w" }] as never,
    stage: "catalog",
    configured: true,
  });
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
  vi.mocked(api.pulseAskStream).mockRejectedValue(
    new ApiError(409, "a question is already running"),
  );
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "hello");
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(await screen.findByTestId("ask-error")).toHaveTextContent(
    /a question is already running/i,
  );
});

test("a follow-up replays the prior turns as history (#522)", async () => {
  answerWith({
    answer: "First answer.",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "one");
  await userEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() =>
    expect(
      within(screen.getByTestId("ask-turns")).getByText("First answer."),
    ).toBeInTheDocument(),
  );

  answerWith({
    answer: "Second answer.",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  await userEvent.type(screen.getByTestId("composer-input"), "two");
  await userEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() =>
    expect(
      within(screen.getByTestId("ask-turns")).getByText("Second answer."),
    ).toBeInTheDocument(),
  );

  expect(api.pulseAskStream).toHaveBeenLastCalledWith(
    "two",
    [
      { role: "user", content: "one" },
      { role: "assistant", content: "First answer." },
    ],
    expect.any(Function),
    expect.any(AbortSignal),
  );
});

test("an unconfigured endpoint disables the control and makes no call (#522)", async () => {
  mount(<AskConsole configured={false} />);
  expect(screen.getByTestId("composer-input")).toBeDisabled();
  expect(screen.getByTestId("composer-send")).toBeDisabled();
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(api.pulseAskStream).not.toHaveBeenCalled();
});

test("the page says these answers are not kept (#878)", async () => {
  answerWith({
    answer: "ok",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "x");
  await userEvent.click(screen.getByTestId("composer-send"));
  // The operator is told, rather than discovering it by reloading and finding nothing. Since #1294
  // the conversation survives navigation (the sidebar keeps it), so the copy names what DOES end
  // it: a reload, or New conversation.
  const note = await screen.findByTestId("ask-transient");
  expect(note).toHaveTextContent(/not kept|disappear/i);
  expect(note).toHaveTextContent(/reload/i);
  expect(note).toHaveTextContent(/new conversation/i);
});

test("a reply that lands AFTER leaving is DISCARDED, and returning finds nothing", async () => {
  // #878's contract on a route. The turns are this component's state, so leaving deletes them —
  // that IS the mechanism, and this asserts its consequences: the late resolution does not throw,
  // does not resurrect the pending turn, and a fresh visit starts empty rather than replaying an
  // answer the operator never waited for.
  let resolveIt: ((v: PulseAskResult) => void) | undefined;
  vi.mocked(api.pulseAskStream).mockImplementation(
    (_q, _h, onEvent) =>
      new Promise<void>((res) => {
        resolveIt = (r) => {
          onEvent({ type: "answer", final: true, ...r });
          res();
        };
      }),
  );

  render(<Host />);
  await userEvent.type(screen.getByTestId("composer-input"), "for this visit");
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(screen.getAllByTestId("ask-turn")).toHaveLength(1);

  // …the operator leaves before the answer comes back.
  await userEvent.click(screen.getByRole("button", { name: "leave" }));
  expect(screen.getByTestId("gone")).toBeInTheDocument();

  resolveIt?.({
    answer: "An answer nobody is waiting for.",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  await Promise.resolve();

  await userEvent.click(screen.getByRole("button", { name: "return" }));
  // A fresh page: no turns at all, and certainly not that answer.
  expect(screen.queryByTestId("ask-turns")).not.toBeInTheDocument();
  expect(
    screen.queryByText("An answer nobody is waiting for."),
  ).not.toBeInTheDocument();
  // …and the box is usable, not stuck busy from a request the previous page made.
  await userEvent.type(screen.getByTestId("composer-input"), "b");
  expect(screen.getByTestId("composer-send")).toBeEnabled();
});

test("StrictMode's effect re-run does not make the page drop every answer", async () => {
  // RED without `live.current = true` in the effect BODY. StrictMode cleans an effect up and runs
  // it again on the same fiber, and `useRef(true)` initialises exactly once — so a cleanup-only
  // liveness flag is false from the first re-run onward and every `then` returns early. The page
  // renders, accepts typing, calls the API, and then silently never answers: the worst shape a
  // regression can take, because nothing errors.
  answerWith({
    answer: "It still answers.",
    matches: [],
    stage: "catalog",
    configured: true,
  });
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
    await within(await screen.findByTestId("ask-turns")).findByText(
      "It still answers.",
    ),
  ).toBeInTheDocument();
});

// ---- #1171: Enter sends, the wait is shown, the answer arrives as it forms --------------------

test("Enter sends; Shift+Enter is a new line; an IME Enter is the composition's", async () => {
  answerWith({ answer: "ok", matches: [], stage: "catalog", configured: true });
  mount(<AskConsole configured />);
  const input = screen.getByTestId("composer-input");
  await userEvent.type(input, "line one{Shift>}{Enter}{/Shift}line two");
  expect(api.pulseAskStream).not.toHaveBeenCalled();
  expect(input).toHaveValue("line one\nline two");
  // Confirming an IME composition fires Enter with isComposing — never a send.
  fireEvent.keyDown(input, { key: "Enter", isComposing: true });
  fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
  expect(api.pulseAskStream).not.toHaveBeenCalled();
  await userEvent.type(input, "{Enter}");
  expect(api.pulseAskStream).toHaveBeenCalledTimes(1);
  expect(vi.mocked(api.pulseAskStream).mock.calls[0][0]).toBe(
    "line one\nline two",
  );
  expect(input).toHaveValue("");
});

test("the wait names its step, shows Stage 1's answer early, then the confirmed one", async () => {
  let emit: ((ev: PulseAskEvent) => void) | undefined;
  let finish: (() => void) | undefined;
  vi.mocked(api.pulseAskStream).mockImplementation(
    (_q, _h, onEvent) =>
      new Promise<void>((res) => {
        emit = onEvent;
        finish = res;
      }),
  );
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "reconnect{Enter}");
  // Before any event: the box is there and says it is working, not a bare ellipsis.
  expect(screen.getByTestId("ask-working")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(/reading your question/i);

  act(() =>
    emit!({ type: "progress", step: "catalog", sessions: 42, missions: 1 }),
  );
  expect(screen.getByRole("status")).toHaveTextContent(
    "Searching 42 sessions and 1 mission…",
  );

  act(() =>
    emit!({
      type: "answer",
      final: false,
      answer: "Probably the ws session.",
      matches: [{ id: "claude:abc", title: "ws", why: "title" }] as never,
      stage: "catalog",
      configured: true,
    }),
  );
  act(() => emit!({ type: "progress", step: "content", candidates: 3 }));
  // Stage 1's answer is on screen WHILE Stage 2 still runs, and the box says what it is doing.
  expect(await screen.findByText("Probably the ws session.")).toBeInTheDocument();
  expect(screen.getByTestId("ask-match")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(
    "Checking against 3 transcripts…",
  );

  act(() =>
    emit!({
      type: "answer",
      final: true,
      answer: "It was the ws session.",
      matches: [{ id: "claude:abc", title: "ws", why: "transcript" }] as never,
      stage: "content",
      configured: true,
    }),
  );
  await act(async () => finish!());
  expect(await screen.findByText("It was the ws session.")).toBeInTheDocument();
  expect(screen.queryByText("Probably the ws session.")).toBeNull();
  expect(screen.queryByTestId("ask-working")).toBeNull();
});

test("a failure after Stage 1 keeps its answer and says what went wrong beside it", async () => {
  vi.mocked(api.pulseAskStream).mockImplementation(async (_q, _h, onEvent) => {
    onEvent({
      type: "answer",
      final: false,
      answer: "First look.",
      matches: [],
      stage: "catalog",
      configured: true,
    });
    throw new ApiError(502, "endpoint returned HTTP 502");
  });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "q{Enter}");
  expect(await screen.findByTestId("ask-error")).toHaveTextContent("HTTP 502");
  expect(screen.getByText("First look.")).toBeInTheDocument();
  expect(screen.queryByTestId("ask-working")).toBeNull();
});

test("the head is New conversation and the sidebar's Close — nothing else (#1294)", async () => {
  answerWith({ answer: "ok", matches: [], stage: "catalog", configured: true });
  const onClose = vi.fn();
  mount(<AskConsole configured onClose={onClose} />);
  const head = screen.getByTestId("ask-head");
  const fresh = within(head).getByRole("button", { name: "New conversation" });
  const close = within(head).getByRole("button", { name: "Close Ask" });
  expect(within(head).getAllByRole("button")).toEqual([fresh, close]);
  // No way "back": Ask is not a page any more.
  expect(within(head).queryAllByRole("link")).toHaveLength(0);
  await userEvent.click(close);
  expect(onClose).toHaveBeenCalledTimes(1);
  // Nothing to start over from yet.
  expect(fresh).toBeDisabled();

  await userEvent.type(screen.getByTestId("composer-input"), "x{Enter}");
  await screen.findByTestId("ask-turns");
  await userEvent.click(fresh);
  expect(screen.queryByTestId("ask-turns")).toBeNull();
  expect(screen.getByTestId("composer-input")).toHaveFocus();
});

test("New conversation ends the ask in flight: its late events never reach the new one", async () => {
  let emit: ((ev: PulseAskEvent) => void) | undefined;
  let signal: AbortSignal | undefined;
  vi.mocked(api.pulseAskStream).mockImplementationOnce(
    (_q, _h, onEvent, s) =>
      new Promise<void>((_res, rej) => {
        emit = onEvent;
        signal = s;
        s?.addEventListener("abort", () =>
          rej(new DOMException("aborted", "AbortError")),
        );
      }),
  );
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "old question{Enter}");
  expect(screen.getByTestId("ask-working")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "New conversation" }));
  // The request was aborted, which frees the server's gate…
  expect(signal?.aborted).toBe(true);
  // …and the box is usable at once, not locked until the old stream ends.
  answerWith({ answer: "new answer", matches: [], stage: "catalog", configured: true });
  await userEvent.type(screen.getByTestId("composer-input"), "new question");
  expect(screen.getByTestId("composer-send")).toBeEnabled();
  await userEvent.type(screen.getByTestId("composer-input"), "{Enter}");
  expect(await screen.findByText("new answer")).toBeInTheDocument();

  // A straggler from the old stream lands nowhere.
  act(() =>
    emit!({
      type: "answer",
      final: true,
      answer: "stale answer",
      matches: [],
      stage: "catalog",
      configured: true,
    }),
  );
  expect(screen.queryByText("stale answer")).toBeNull();
  expect(screen.queryByText("old question")).toBeNull();
  expect(screen.getAllByTestId("ask-turn")).toHaveLength(1);
  expect(screen.queryByTestId("ask-error")).toBeNull();
});

test("leaving the page aborts the ask in flight", async () => {
  let signal: AbortSignal | undefined;
  vi.mocked(api.pulseAskStream).mockImplementation(
    (_q, _h, _on, s) =>
      new Promise<void>(() => {
        signal = s;
      }),
  );
  render(<Host />);
  await userEvent.type(screen.getByTestId("composer-input"), "q{Enter}");
  expect(signal?.aborted).toBe(false);
  await userEvent.click(screen.getByRole("button", { name: "leave" }));
  await waitFor(() => expect(signal?.aborted).toBe(true));
});

test("an aborted ask that settles LATE does not unlock the box under the next question", async () => {
  // The aborted request here ignores its signal and settles only after the next question has
  // started — the order a slow transport can produce. Only the CURRENT ask may clear `busy`.
  let settleOld: (() => void) | undefined;
  vi.mocked(api.pulseAskStream)
    .mockImplementationOnce(
      () =>
        new Promise<void>((res) => {
          settleOld = res;
        }),
    )
    .mockImplementationOnce(() => new Promise<void>(() => {})); // the next question: in flight
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "old{Enter}");
  await userEvent.click(screen.getByRole("button", { name: "New conversation" }));
  await userEvent.type(screen.getByTestId("composer-input"), "new{Enter}");
  expect(screen.getByTestId("ask-working")).toBeInTheDocument();

  await act(async () => settleOld!());
  await userEvent.type(screen.getByTestId("composer-input"), "third");
  // Still busy with "new": the late settle of "old" must not have re-enabled Send.
  expect(screen.getByTestId("composer-send")).toBeDisabled();
  expect(screen.getByTestId("ask-working")).toBeInTheDocument();
});
