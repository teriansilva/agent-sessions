import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { AgentEndpoint, ChatSession, ChatTurn, EngineInfo } from "../../types/api";
import { RuntimeGate } from "../terminal/RuntimeGate";
import { BUSY_BACKOFF_MAX_MS, BUSY_BACKOFF_MS, CHAT_IDLE_POLL_MS, CHAT_POLL_MS, ChatPane } from "./ChatPane";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      chatGet: vi.fn(),
      chatSend: vi.fn(),
      chatRetry: vi.fn(),
      agentEndpoint: vi.fn(),
    },
  };
});

const SID = "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b";
const ENDPOINT: AgentEndpoint = {
  base_url: "https://llm.example.lan/v1",
  model: "qwen3-coder",
  api_key_set: true,
  context_window: 32768,
  max_output_tokens: 4096,
  request_timeout: null,
  configured: true,
  tools: "none",
};

function turn(over: Partial<ChatTurn>): ChatTurn {
  return {
    turn_id: crypto.randomUUID(),
    text: "hello",
    ts: Date.now() / 1000,
    status: "done",
    reason: null,
    reply: "hi there",
    reply_ts: Date.now() / 1000,
    usage: { total_tokens: 1900 },
    truncated: false,
    dropped: 0,
    ...over,
  };
}

function session(turns: ChatTurn[]): ChatSession {
  return {
    session_id: SID,
    cwd: "/w",
    created_at: 0,
    turns,
    in_flight: turns.find((t) => t.status === "pending")?.turn_id ?? null,
  };
}

function renderPane() {
  return render(
    <MemoryRouter>
      <ChatPane engine="apichat" id={SID} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  setRoster(fixture.engines as EngineInfo[], []);
  vi.mocked(api.agentEndpoint).mockResolvedValue(ENDPOINT);
  vi.mocked(api.chatGet).mockResolvedValue(session([]));
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  resetRoster();
});

test("an empty conversation explains itself, and Enter sends with a fresh turn id", async () => {
  renderPane();
  expect(await screen.findByTestId("chat-empty")).toHaveTextContent(/it has no tools/i);
  expect(await screen.findByTestId("chat-model")).toHaveTextContent("qwen3-coder");
  vi.mocked(api.chatSend).mockResolvedValue({ turn: { turn_id: "x", status: "pending" } });
  const pending = turn({ text: "why?", status: "pending", reply: null, usage: null });
  vi.mocked(api.chatGet).mockResolvedValue(session([pending]));
  await userEvent.type(screen.getByLabelText("Message the agent"), "why?{Enter}");
  expect(api.chatSend).toHaveBeenCalledTimes(1);
  const [sid, turnId, text] = vi.mocked(api.chatSend).mock.calls[0];
  expect(sid).toBe(`apichat:${SID}`);
  expect(turnId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(text).toBe("why?");
  expect(await screen.findByTestId("chat-waiting")).toHaveTextContent(/waiting for the endpoint/i);
  expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
});

test("a pending turn is re-read until it settles — the pane reads, it never resends", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const pending = turn({ text: "slow", status: "pending", reply: null, usage: null });
  vi.mocked(api.chatGet).mockResolvedValue(session([pending]));
  renderPane();
  expect(await screen.findByTestId("chat-waiting")).toBeInTheDocument();
  vi.mocked(api.chatGet).mockResolvedValue(
    session([{ ...pending, status: "done", reply: "done now", reply_ts: Date.now() / 1000 }]),
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CHAT_POLL_MS + 10);
  });
  expect(await screen.findByText("done now")).toBeInTheDocument();
  expect(api.chatSend).not.toHaveBeenCalled();
});

test("a failed turn keeps the message, says why, and Retry resends that same turn", async () => {
  const failed = turn({
    status: "failed",
    reply: null,
    usage: null,
    reason:
      "the endpoint did not answer within 120s — it may already have processed (and billed) this request; Retry sends it again",
  });
  vi.mocked(api.chatGet).mockResolvedValue(session([failed]));
  vi.mocked(api.chatRetry).mockResolvedValue({ turn: { turn_id: failed.turn_id, status: "pending" } });
  renderPane();
  expect(await screen.findByTestId("chat-failed")).toHaveTextContent(/may already have processed/);
  expect(screen.getByTestId("chat-failed")).toHaveTextContent(/your message was kept/i);
  await userEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(api.chatRetry).toHaveBeenCalledWith(`apichat:${SID}`, failed.turn_id);
});

test("the truncation and dropped-history notices are shown", async () => {
  vi.mocked(api.chatGet).mockResolvedValue(
    session([turn({ dropped: 3, truncated: true, reply: "partial" })]),
  );
  renderPane();
  expect(await screen.findByTestId("chat-dropped")).toHaveTextContent(/3 earlier turns were not sent/);
  expect(screen.getByTestId("chat-truncated")).toHaveTextContent(/output limit/);
});

test("an unconfigured agent says nothing is sent, and links to its settings", async () => {
  vi.mocked(api.agentEndpoint).mockResolvedValue({ ...ENDPOINT, configured: false, api_key_set: false });
  renderPane();
  expect(await screen.findByTestId("chat-unconfigured")).toHaveTextContent(/nothing is sent/i);
  expect(screen.getByRole("link", { name: /open agent settings/i })).toHaveAttribute(
    "href",
    "/settings/agents/apichat",
  );
});

test("a refused send leaves the composer at once and loses nothing: Edit puts it back (#1465)", async () => {
  vi.mocked(api.chatSend).mockRejectedValue(
    new ApiError(413, "too long for this endpoint's context window"),
  );
  renderPane();
  await screen.findByTestId("chat-empty");
  const box = screen.getByLabelText("Message the agent");
  await userEvent.type(box, "a very long message{Enter}");
  expect(box).toHaveValue("");
  const pending = await screen.findByTestId("structured-pending");
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "refused"));
  expect(pending).toHaveTextContent("Not sent: too long for this endpoint's context window");
  expect(within(pending).queryByRole("button", { name: "Retry" })).toBeNull();
  await userEvent.click(within(pending).getByRole("button", { name: "Edit" }));
  expect(box).toHaveValue("a very long message");
  expect(screen.queryByTestId("structured-pending")).toBeNull();
});

test("a reply is text: markup in it is shown, never rendered", async () => {
  vi.mocked(api.chatGet).mockResolvedValue(
    session([turn({ reply: '<img src=x onerror="alert(1)"> and ```\ncode()\n``` done' })]),
  );
  const { container } = renderPane();
  expect(await screen.findByText(/<img src=x onerror/)).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("pre")).toHaveTextContent("code()");
});

// ---- asynchronous state boundaries (Hermes on #1219) ---------------------------------------------

test("switching conversations never carries the draft — or a late reply — into the other one", async () => {
  const OTHER = "0190a3b2-1c2d-7e3f-8a9b-000000000000";
  const { rerender } = render(
    <MemoryRouter>
      <RuntimeGate engine="apichat" id={SID}>
        TERMINAL
      </RuntimeGate>
    </MemoryRouter>,
  );
  await screen.findByTestId("chat-empty");
  await userEvent.type(screen.getByLabelText("Message the agent"), "meant for A");
  let lateA!: (s: ChatSession) => void;
  vi.mocked(api.chatGet).mockImplementation((sid: string) =>
    sid === `apichat:${SID}`
      ? new Promise((r) => {
          lateA = r;
        })
      : Promise.resolve(session([turn({ text: "B's own", reply: "B reply" })])),
  );
  rerender(
    <MemoryRouter>
      <RuntimeGate engine="apichat" id={OTHER}>
        TERMINAL
      </RuntimeGate>
    </MemoryRouter>,
  );
  expect(await screen.findByText("B reply")).toBeInTheDocument();
  expect(screen.getByLabelText("Message the agent")).toHaveValue("");
  lateA?.(session([turn({ text: "A's", reply: "A reply" })]));
  await new Promise((r) => setTimeout(r, 0));
  expect(screen.queryByText("A reply")).toBeNull();
});

test("a send whose response was lost but which the server took is not offered again", async () => {
  vi.mocked(api.chatSend).mockRejectedValue(new TypeError("Failed to fetch"));
  vi.mocked(api.chatGet).mockImplementation(async () => {
    const [, turnId, text] = vi.mocked(api.chatSend).mock.calls.at(-1) ?? [];
    return turnId ? session([turn({ turn_id: turnId, text, reply: "answered" })]) : session([]);
  });
  renderPane();
  await screen.findByTestId("chat-empty");
  await userEvent.type(screen.getByLabelText("Message the agent"), "only once{Enter}");
  expect(await screen.findByText("answered")).toBeInTheDocument();
  expect(screen.getByLabelText("Message the agent")).toHaveValue("");
  expect(screen.queryByTestId("chat-send-error")).toBeNull();
});

test("an ambiguous failure the server did not take offers Retry under the same turn id, never Edit (#1465)", async () => {
  vi.mocked(api.chatSend).mockRejectedValueOnce(new TypeError("Failed to fetch"));
  renderPane();
  await screen.findByTestId("chat-empty");
  const box = screen.getByLabelText("Message the agent");
  await userEvent.type(box, "maybe sent{Enter}");
  const pending = await screen.findByTestId("structured-pending");
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "unknown"));
  expect(pending).toHaveTextContent("May not have reached BattleLab");
  expect(within(pending).queryByRole("button", { name: "Edit" })).toBeNull();
  expect(within(pending).getByRole("button", { name: "Discard — it may still arrive" })).toBeVisible();
  vi.mocked(api.chatSend).mockResolvedValueOnce({ turn: { turn_id: "x", status: "pending" } });
  await userEvent.click(within(pending).getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(api.chatSend).toHaveBeenCalledTimes(2));
  const [first, second] = vi.mocked(api.chatSend).mock.calls;
  expect(second[1]).toBe(first[1]); // the same turn: the server dedupes it
  expect(second[2]).toBe("maybe sent");
});

test("the composer is free while a send settles; newer typing is never overwritten (#1465)", async () => {
  let reject!: (e: unknown) => void;
  vi.mocked(api.chatSend).mockReturnValue(
    new Promise((_, r) => {
      reject = r;
    }),
  );
  renderPane();
  await screen.findByTestId("chat-empty");
  const box = screen.getByLabelText("Message the agent");
  await userEvent.type(box, "first{Enter}");
  expect(box).not.toHaveAttribute("readonly");
  await userEvent.type(box, "second");
  expect(box).toHaveValue("second");
  await act(async () => reject(new ApiError(413, "too long for this endpoint's context window")));
  await waitFor(() => expect(screen.getByTestId("structured-pending")).toHaveAttribute("data-phase", "refused"));
  expect(box).toHaveValue("second"); // the refusal stays in its entry
  expect(within(screen.getByTestId("structured-pending")).getByRole("button", { name: "Edit" })).toBeDisabled();
});

// ---- the composer's outbox (#1465) -------------------------------------------------------------

/** A tiny chat server: `turns` is the conversation; a send records a pending turn. */
function chatServer(initial: ChatTurn[] = []) {
  const server = {
    turns: initial,
    finish() {
      server.turns = server.turns.map((t) => (t.status === "pending" ? { ...t, status: "done", reply: "ok" } : t));
    },
  };
  vi.mocked(api.chatGet).mockImplementation(async () => session(server.turns));
  vi.mocked(api.chatSend).mockImplementation(async (_sid, turn_id, text) => {
    if (server.turns.some((t) => t.status === "pending")) {
      throw new ApiError(409, "another message is still waiting for its reply");
    }
    server.turns = [...server.turns, turn({ turn_id, text, status: "pending", reply: null, usage: null })];
    return { turn: { turn_id, status: "pending" } };
  });
  return server;
}

test("two sends during a slow reply wait for it and go out one reply at a time, in order", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const server = chatServer([turn({ text: "slow", status: "pending", reply: null, usage: null })]);
  renderPane();
  await screen.findByTestId("chat-waiting");
  const box = screen.getByLabelText("Message the agent");
  await userEvent.type(box, "second{Enter}");
  await userEvent.type(box, "third{Enter}");
  expect(box).toHaveValue("");
  const [a, b] = screen.getAllByTestId("structured-pending");
  expect(a).toHaveTextContent("Goes out when the current reply has finished.");
  expect(b).toHaveTextContent("Goes out after the message above.");
  expect(api.chatSend).not.toHaveBeenCalled();
  server.finish();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CHAT_POLL_MS + 10);
  });
  await waitFor(() => expect(api.chatSend).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.chatSend).mock.calls[0][2]).toBe("second");
  await waitFor(() => expect(screen.getAllByTestId("structured-pending")).toHaveLength(1));
  server.finish();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CHAT_POLL_MS + 10);
  });
  await waitFor(() => expect(api.chatSend).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.chatSend).mock.calls[1][2]).toBe("third");
  await waitFor(() => expect(screen.queryByTestId("structured-pending")).toBeNull());
  expect(screen.queryByText(/another message is still waiting/)).toBeNull(); // never refused
});

test("a 409 because another reply started meanwhile is not a refusal: the message waits, same id", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const server = chatServer();
  renderPane();
  await screen.findByTestId("chat-empty");
  // Another tab starts a reply after this one's last read.
  server.turns = [turn({ text: "from another tab", status: "pending", reply: null, usage: null })];
  await userEvent.type(screen.getByLabelText("Message the agent"), "mine{Enter}");
  await waitFor(() => expect(api.chatSend).toHaveBeenCalledTimes(1));
  const pending = await screen.findByTestId("structured-pending");
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "waiting"));
  server.finish();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CHAT_POLL_MS + 10);
  });
  await waitFor(() => expect(api.chatSend).toHaveBeenCalledTimes(2));
  const [first, second] = vi.mocked(api.chatSend).mock.calls;
  expect(second[1]).toBe(first[1]);
});

test("a Retry the server answers 409 in_flight is recorded, never refused (it holds that turn)", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.chatSend)
    .mockRejectedValueOnce(new TypeError("Failed to fetch"))
    .mockRejectedValueOnce(new ApiError(409, "in_flight"));
  renderPane();
  await screen.findByTestId("chat-empty");
  vi.mocked(api.chatGet).mockRejectedValue(new TypeError("offline")); // reads fail meanwhile
  await userEvent.type(screen.getByLabelText("Message the agent"), "maybe sent{Enter}");
  const pending = await screen.findByTestId("structured-pending");
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "unknown"));
  await userEvent.click(within(pending).getByRole("button", { name: "Retry" }));
  // #1570 follow-up: while reads fail the Retry waits in the outbox rather than posting into a dead
  // link; the first good read releases it.
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "waiting"));
  expect(api.chatSend).toHaveBeenCalledTimes(1);
  vi.mocked(api.chatGet).mockResolvedValue(session([]));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CHAT_IDLE_POLL_MS + 10);
  });
  await waitFor(() => expect(pending).toHaveTextContent("Recorded by BattleLab"));
  expect(pending).toHaveAttribute("data-phase", "sending");
  expect(within(pending).queryByRole("button", { name: "Edit" })).toBeNull();
});

test("Edit on a refused message releases the queue; the edit goes after it as a new id", async () => {
  chatServer();
  const inner = vi.mocked(api.chatSend).getMockImplementation()!;
  let held!: () => void;
  vi.mocked(api.chatSend).mockImplementationOnce(
    () => new Promise((_, reject) => (held = () => reject(new ApiError(422, "text must be a non-empty string")))),
  );
  renderPane();
  await screen.findByTestId("chat-empty");
  const box = screen.getByLabelText("Message the agent");
  await userEvent.type(box, "first{Enter}");
  await userEvent.type(box, "second{Enter}");
  await act(async () => held());
  const [refused, waiting] = screen.getAllByTestId("structured-pending");
  await waitFor(() => expect(refused).toHaveAttribute("data-phase", "refused"));
  expect(waiting).toHaveAttribute("data-phase", "waiting"); // the order holds behind a failure
  expect(api.chatSend).toHaveBeenCalledTimes(1);
  vi.mocked(api.chatSend).mockImplementation(inner);
  await userEvent.click(within(refused).getByRole("button", { name: "Edit" }));
  expect(box).toHaveValue("first");
  await waitFor(() => expect(vi.mocked(api.chatSend).mock.calls.map((c) => c[2])).toEqual(["first", "second"]));
  const firstId = vi.mocked(api.chatSend).mock.calls[0][1];
  // The second's reply runs now; the edit waits for it, then goes as a NEW turn.
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(screen.getByTestId("structured-pending")).toHaveTextContent("Goes out when the current reply has finished.");
  expect(screen.getByTestId("structured-pending").getAttribute("data-operation")).not.toBe(firstId);
});

test("leaving the page asks first only while something is unrecorded", async () => {
  chatServer();
  let resolve!: () => void;
  vi.mocked(api.chatSend).mockImplementationOnce(() => new Promise((r) => (resolve = () => r({ turn: { turn_id: "x", status: "pending" } }))));
  renderPane();
  await screen.findByTestId("chat-empty");
  const prompts = () => {
    const ev = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(ev);
    return ev.defaultPrevented;
  };
  expect(prompts()).toBe(false);
  await userEvent.type(screen.getByLabelText("Message the agent"), "first{Enter}");
  expect(prompts()).toBe(true);
  expect(screen.getByTestId("structured-outbox-note")).toHaveTextContent("Leaving drops this list");
  await act(async () => resolve());
  await waitFor(() => expect(prompts()).toBe(false));
});

test("a pane left with a send in flight sends nothing more when it answers late", async () => {
  const server = chatServer();
  let resolve!: () => void;
  vi.mocked(api.chatSend).mockImplementationOnce((_s, turn_id, text) => new Promise((r) => (resolve = () => {
    server.turns = [...server.turns, turn({ turn_id, text, status: "done" })];
    r({ turn: { turn_id, status: "pending" } });
  })));
  const view = renderPane();
  await screen.findByTestId("chat-empty");
  const box = screen.getByLabelText("Message the agent");
  await userEvent.type(box, "first{Enter}");
  await userEvent.type(box, "second{Enter}");
  view.unmount();
  await act(async () => resolve());
  await act(async () => {});
  expect(api.chatSend).toHaveBeenCalledTimes(1);
});

test("a failed re-read keeps the conversation and its pending entries in view", async () => {
  const server = chatServer();
  vi.mocked(api.chatSend).mockRejectedValue(new TypeError("Failed to fetch"));
  renderPane();
  await screen.findByTestId("chat-empty");
  vi.mocked(api.chatGet).mockRejectedValue(new TypeError("offline"));
  await userEvent.type(screen.getByLabelText("Message the agent"), "first{Enter}");
  const pending = await screen.findByTestId("structured-pending");
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "unknown"));
  expect(await screen.findByTestId("chat-reconnecting")).toBeInTheDocument();
  expect(screen.getByTestId("chat-pane")).toBeInTheDocument();
  void server;
});

// ---- read tools (#1222) ----------------------------------------------------------------------------

test("each tool call is one summary row: listed, read with its span, refused with the reason", async () => {
  const long = `src/${"deeply/nested/".repeat(12)}module_with_a_long_name.py`;
  vi.mocked(api.chatGet).mockResolvedValue(
    session([
      turn({
        reply: "done",
        tools: [
          { call_id: "a", name: "list_files", path: "src", outcome: "ok", entries: 42 },
          { call_id: "b", name: "read_file", path: long, outcome: "ok", start_line: 1, end_line: 200, total_lines: 812 },
          { call_id: "c", name: "read_file", path: ".env", outcome: "refused", reason: "hidden path — never readable by the agent" },
          { call_id: "d", name: "read_file", path: "x.py", outcome: "stopped" },
        ],
      }),
    ]),
  );
  renderPane();
  const rows = await screen.findAllByTestId("chat-tool");
  expect(rows.map((r) => r.textContent)).toEqual([
    "Listedsrc42 entries",
    `Read${long}lines 1–200 of 812`,
    "Refused.envhidden path — never readable by the agent",
    "Stoppedx.pydid not finish",
  ]);
  expect(rows[2]).toHaveAttribute("data-outcome", "refused");
  expect(screen.getByRole("list", { name: "Files the agent looked at" })).toBeInTheDocument();
});

test("a running call names itself in the waiting row", async () => {
  vi.mocked(api.chatGet).mockResolvedValue(
    session([
      turn({
        status: "pending",
        reply: null,
        usage: null,
        tools: [{ call_id: "a", name: "read_file", path: "src/app.py", outcome: "running" }],
      }),
    ]),
  );
  renderPane();
  expect(await screen.findByTestId("chat-waiting")).toHaveTextContent(/reading src\/app\.py/);
});

test.each([
  ["none", /It has no tools/],
  ["read", /list and read files in this conversation’s folder/],
] as const)("the empty conversation says which tools mode is on (%s)", async (tools, copy) => {
  vi.mocked(api.agentEndpoint).mockResolvedValue({ ...ENDPOINT, tools });
  renderPane();
  await waitFor(() => expect(screen.getByTestId("chat-empty")).toHaveTextContent(copy));
});

// #1502 (Hermes on #1522): a chat pane drawn from a CACHED runtime hint the roster has not
// confirmed may be the wrong pane — the engine may be an API engine now, whose session the chat
// route refuses. Until the roster confirms the runtime, a failed first read is "Opening…" (bounded
// by Retry, owned by the gate), never "Couldn't load this conversation."
test("an unconfirmed runtime makes a failed read provisional; a confirmed one shows the error (#1502)", async () => {
  vi.mocked(api.chatGet).mockRejectedValue(new ApiError(409, "not a chat session"));
  const view = render(
    <MemoryRouter>
      <ChatPane engine="apichat" id={SID} runtimeConfirmed={false} />
    </MemoryRouter>,
  );
  expect(await screen.findByText("Opening this session…")).toBeInTheDocument();
  expect(screen.queryByText("Couldn’t load this conversation.")).toBeNull();
  view.rerender(
    <MemoryRouter>
      <ChatPane engine="apichat" id={SID} runtimeConfirmed />
    </MemoryRouter>,
  );
  expect(await screen.findByText("Couldn’t load this conversation.")).toBeInTheDocument();
});

test("an unconfirmed runtime does not hide a conversation that loads (#1502)", async () => {
  render(
    <MemoryRouter>
      <ChatPane engine="apichat" id={SID} runtimeConfirmed={false} />
    </MemoryRouter>,
  );
  expect(await screen.findByTestId("chat-empty")).toBeInTheDocument();
  expect(screen.queryByText("Opening this session…")).toBeNull();
});

// ---- busy conflicts (Hermes on #1514) -----------------------------------------------------------

test("a fence 409 raised before the turn is appended is busy, not a refusal, even when the read shows nothing in flight", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  chatServer();
  const inner = vi.mocked(api.chatSend).getMockImplementation()!;
  vi.mocked(api.chatSend).mockRejectedValueOnce(new ApiError(409, "in_flight — another worker owns this conversation"));
  renderPane();
  await screen.findByTestId("chat-empty");
  await userEvent.type(screen.getByLabelText("Message the agent"), "mine{Enter}");
  const pending = await screen.findByTestId("structured-pending");
  await waitFor(() => expect(api.chatSend).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "waiting")); // never "refused"
  vi.mocked(api.chatSend).mockImplementation(inner);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(BUSY_BACKOFF_MS + 50); // the fresh read after the backoff
  });
  await waitFor(() => expect(api.chatSend).toHaveBeenCalledTimes(2));
  const [first, second] = vi.mocked(api.chatSend).mock.calls;
  expect(second[1]).toBe(first[1]);
  await waitFor(() => expect(screen.queryByTestId("structured-pending")).toBeNull());
});

test("a busy 409 while reads fail never loops: POSTs stay bounded, then it is delivered once both recover", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const server = chatServer();
  const inner = vi.mocked(api.chatSend).getMockImplementation()!;
  vi.mocked(api.chatSend).mockRejectedValue(new ApiError(409, "in_flight — another worker owns this conversation"));
  renderPane();
  await screen.findByTestId("chat-empty"); // the last good read: idle
  vi.mocked(api.chatGet).mockRejectedValue(new TypeError("offline"));
  await userEvent.type(screen.getByLabelText("Message the agent"), "mine{Enter}");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });
  expect(vi.mocked(api.chatSend).mock.calls.length).toBeLessThanOrEqual(1); // no POST without a fresh read
  expect(screen.getByTestId("structured-pending")).toHaveAttribute("data-phase", "waiting");
  // Both recover: the next scheduled read admits it, and it goes once.
  vi.mocked(api.chatGet).mockImplementation(async () => session(server.turns));
  vi.mocked(api.chatSend).mockImplementation(inner);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(BUSY_BACKOFF_MAX_MS + 100);
  });
  await waitFor(() => expect(screen.queryByTestId("structured-pending")).toBeNull());
  expect(vi.mocked(api.chatSend).mock.calls.length).toBe(2);
  expect(server.turns.filter((t) => t.text === "mine")).toHaveLength(1);
});

// ---- #1570 follow-up: the uplink veil covers chat sessions too — one veil, every session ----

test("an idle chat pane re-reads, so a lost link shows the uplink veil after 5 s; it clears on recovery", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.chatGet).mockResolvedValue(session([]));
  renderPane();
  await screen.findByTestId("chat-empty");
  vi.mocked(api.chatGet).mockClear();
  vi.mocked(api.chatGet).mockRejectedValue(new TypeError("offline"));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CHAT_IDLE_POLL_MS + 10); // the idle re-read fails
  });
  expect(api.chatGet).toHaveBeenCalledTimes(1); // nothing running, yet it read
  expect(await screen.findByTestId("chat-reconnecting")).toBeInTheDocument();
  expect(screen.queryByTestId("uplink-veil")).toBeNull(); // inside the 5 s grace
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });
  const veil = await screen.findByTestId("uplink-veil");
  expect(veil).toHaveAttribute("data-phase", "lost");
  expect(veil).toHaveTextContent("wait in the outbox");
  expect(screen.getByTestId("api-log").contains(veil)).toBe(false);
  expect(screen.getByTestId("api-log")).toHaveAttribute("aria-busy", "true");
  expect(screen.getByTestId("chat-reconnecting")).not.toHaveAttribute("role"); // one announcement source
  vi.mocked(api.chatGet).mockResolvedValue(session([]));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CHAT_IDLE_POLL_MS + 10);
  });
  await waitFor(() => expect(screen.queryByTestId("chat-reconnecting")).toBeNull());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_000); // the 750 ms hold, RESTORED, the fade
  });
  expect(screen.queryByTestId("uplink-veil")).toBeNull();
});

test("while chat re-reads fail a new message waits in the outbox; the first good read sends it", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  chatServer();
  renderPane();
  await screen.findByTestId("chat-empty");
  const good = vi.mocked(api.chatGet).getMockImplementation()!;
  vi.mocked(api.chatGet).mockRejectedValue(new TypeError("offline"));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CHAT_IDLE_POLL_MS + 10);
  });
  await screen.findByTestId("chat-reconnecting");
  await userEvent.type(screen.getByLabelText("Message the agent"), "held{Enter}");
  const pending = await screen.findByTestId("structured-pending");
  expect(pending).toHaveAttribute("data-phase", "waiting");
  expect(api.chatSend).not.toHaveBeenCalled();
  vi.mocked(api.chatGet).mockImplementation(good);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CHAT_IDLE_POLL_MS + 10);
  });
  await waitFor(() => expect(api.chatSend).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.chatSend).mock.calls[0][2]).toBe("held");
});

test("a new chat pane whose first read fails transiently shows ESTABLISHING, retries, and opens when the link is up (Hermes on #1592)", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.chatGet).mockRejectedValue(new ApiError(503, "unavailable"));
  renderPane();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_100);
  });
  const veil = await screen.findByTestId("uplink-veil");
  expect(veil).toHaveAttribute("data-phase", "establishing");
  expect(screen.queryByText("Couldn’t load this conversation.")).toBeNull(); // not an answer
  expect(vi.mocked(api.chatGet).mock.calls.length).toBeGreaterThan(1); // it re-reads
  vi.mocked(api.chatGet).mockResolvedValue(session([]));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CHAT_POLL_MS + 10);
  });
  expect(await screen.findByTestId("chat-empty")).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_000); // the hold, RESTORED, the fade
  });
  expect(screen.queryByTestId("uplink-veil")).toBeNull();
});

test("a definite refusal on the first read is still the server's answer, never a veil", async () => {
  vi.mocked(api.chatGet).mockRejectedValue(new ApiError(404, "no such chat"));
  renderPane();
  expect(await screen.findByText("This conversation does not exist (or was never started).")).toBeInTheDocument();
  expect(screen.queryByTestId("uplink-veil")).toBeNull();
});
