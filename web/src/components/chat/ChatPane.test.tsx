import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { AgentEndpoint, ChatSession, ChatTurn, EngineInfo } from "../../types/api";
import { RuntimeGate } from "../terminal/RuntimeGate";
import { CHAT_POLL_MS, ChatPane } from "./ChatPane";

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

test("a refused send puts the message back and shows the reason", async () => {
  vi.mocked(api.chatSend).mockRejectedValue(
    new ApiError(413, "too long for this endpoint's context window"),
  );
  renderPane();
  await screen.findByTestId("chat-empty");
  const box = screen.getByLabelText("Message the agent");
  await userEvent.type(box, "a very long message{Enter}");
  expect(await screen.findByTestId("chat-send-error")).toHaveTextContent(/context window/);
  await waitFor(() => expect(box).toHaveValue("a very long message"));
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

test("an ambiguous failure the server did not take keeps the message, and resending reuses its turn id", async () => {
  vi.mocked(api.chatSend).mockRejectedValueOnce(new TypeError("Failed to fetch"));
  renderPane();
  await screen.findByTestId("chat-empty");
  const box = screen.getByLabelText("Message the agent");
  await userEvent.type(box, "maybe sent{Enter}");
  expect(await screen.findByTestId("chat-send-error")).toHaveTextContent(/will not be duplicated/);
  await waitFor(() => expect(box).toHaveValue("maybe sent"));
  vi.mocked(api.chatSend).mockResolvedValueOnce({ turn: { turn_id: "x", status: "pending" } });
  await userEvent.type(box, "{Enter}");
  const [first, second] = vi.mocked(api.chatSend).mock.calls;
  expect(second[1]).toBe(first[1]); // the same turn: the server dedupes it
});

test("the composer is read-only while a send settles, so a refusal cannot overwrite newer typing", async () => {
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
  expect(box).toHaveAttribute("readonly");
  await userEvent.type(box, "second");
  expect(box).toHaveValue("");
  reject(new ApiError(413, "too long for this endpoint's context window"));
  await waitFor(() => expect(box).toHaveValue("first"));
  expect(box).not.toHaveAttribute("readonly");
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
