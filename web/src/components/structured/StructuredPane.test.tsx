import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import { imageFilesFromAsyncClipboard } from "../../lib/clipboardImages";
import { appendSent, clearSent, readSent } from "../../lib/sentHistory";
import fixture from "../../test/roster.fixture.json";
import type {
  EngineInfo,
  StructuredRequest,
  StructuredSnapshot,
  StructuredTurn,
  Template,
} from "../../types/api";
import { RuntimeGate } from "../terminal/RuntimeGate";
import { StructuredPane } from "./StructuredPane";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      structuredSnapshot: vi.fn(),
      structuredEvents: vi.fn(),
      structuredSubmit: vi.fn(),
      structuredDecide: vi.fn(),
      structuredInterrupt: vi.fn(),
      structuredStop: vi.fn(),
      structuredStart: vi.fn(),
      structuredEnableBypass: vi.fn(),
      upload: vi.fn(),
      templates: vi.fn(),
      templateVariables: vi.fn(() => Promise.resolve({ variables: [], limits: {} })),
      uploadBlob: vi.fn(() => new Promise(() => {})),
      // The session brief reads the lookup for the model it shows (#1511).
      session: vi.fn(() => new Promise(() => {})),
    },
  };
});

vi.mock("../../lib/clipboardImages", async () => {
  const actual = await vi.importActual<typeof import("../../lib/clipboardImages")>(
    "../../lib/clipboardImages",
  );
  return { ...actual, imageFilesFromAsyncClipboard: vi.fn(async () => []) };
});

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `codex-api:${ID}`;

function turn(over: Partial<StructuredTurn> = {}): StructuredTurn {
  return {
    turn_id: crypto.randomUUID(),
    operation_id: "",
    state: "completed",
    text: "hello",
    reply: "echo:hello",
    text_truncated: false,
    reply_truncated: false,
    reason: null,
    tools: [],
    tools_truncated: false,
    ...over,
  };
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
    turns: [],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n", worker: "w1", background_active: false },
    read_only: null,
    ...over,
  };
}

function renderPane(engine = "codex-api") {
  return render(
    <MemoryRouter>
      <StructuredPane engine={engine} id={ID} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  setRoster(fixture.engines as EngineInfo[], []);
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap());
  vi.mocked(api.structuredEvents).mockResolvedValue({ session_key: KEY, revision: 4, next_cursor: 4, events: [] });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  resetRoster();
});

test("RuntimeGate opens an api engine in the structured pane, never a terminal", async () => {
  render(
    <MemoryRouter>
      <RuntimeGate engine="codex-api" id={ID}>
        <div data-testid="terminal" />
      </RuntimeGate>
    </MemoryRouter>,
  );
  expect(await screen.findByTestId("structured-pane")).toBeInTheDocument();
  expect(screen.queryByTestId("terminal")).toBeNull();
});

test("an unstarted skip session offers Start or Discard and takes no message (#1339)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({ bypass: true, pending_start: true, start_expires_at: Date.now() / 1000 + 600, native: undefined }),
  );
  vi.mocked(api.structuredStart).mockResolvedValue(snap({ bypass: true, pending_start: false }));
  renderPane();
  const panel = await screen.findByTestId("structured-pending-start");
  expect(panel).toHaveTextContent("Nothing has run");
  expect(screen.getByRole("textbox", { name: /message/i })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Start (skips prompts)" }));
  expect(api.structuredStart).toHaveBeenCalledWith(KEY);
  expect(api.structuredStop).not.toHaveBeenCalled();
});

test("an interrupted start still offers Start, saying it didn't finish (Hermes 5908)", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({ bypass: true, pending_start: true, start_incomplete: true, native: undefined }),
  );
  renderPane();
  const panel = await screen.findByTestId("structured-pending-start");
  expect(panel).toHaveTextContent("didn’t finish");
  expect(screen.getByRole("button", { name: "Start (skips prompts)" })).toBeEnabled();
});

test("Discard on an unstarted skip session stops it; an expired one says it never ran (#1339)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({ bypass: true, pending_start: true, start_expires_at: Date.now() / 1000 + 600, native: undefined }),
  );
  vi.mocked(api.structuredStop).mockResolvedValue({ containment: "gone" });
  const view = renderPane();
  await user.click(await screen.findByRole("button", { name: "Discard" }));
  expect(api.structuredStop).toHaveBeenCalledWith(KEY);
  expect(api.structuredStart).not.toHaveBeenCalled();
  view.unmount();
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({ bypass: true, pending_start: false, start_expired: true, native: undefined }),
  );
  renderPane();
  expect(await screen.findByTestId("structured-start-expired")).toHaveTextContent("never ran");
  expect(screen.queryByTestId("structured-pending-start")).toBeNull();
});

test("a skip-permissions session says so in the head; a guarded one does not (#1339)", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ bypass: true }));
  const view = renderPane();
  expect(await screen.findByTestId("structured-bypass")).toHaveTextContent(/skip permissions/i);
  view.unmount();
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap());
  renderPane();
  await screen.findByTestId("structured-worker");
  expect(screen.queryByTestId("structured-bypass")).toBeNull();
});

test("a command approval shows its complete request and only the server's choices", async () => {
  const t = turn({ state: "awaiting_approval", reply: "" });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({
      state: "awaiting_approval",
      active_turn: t.turn_id,
      turns: [t],
      pending_requests: [
        {
          request_id: "7",
          turn_id: t.turn_id,
          kind: "command",
          choices: ["approve", "reject"],
          payload: { command: "npm test", cwd: "/w", networkApprovalContext: { host: "x" } },
          payload_digest: "abcdef0123",
          complete: true,
        },
      ],
    }),
  );
  renderPane();
  const card = await screen.findByTestId("structured-request");
  const fields = within(card).getAllByTestId("structured-field").map((f) => f.dataset.field);
  expect(fields).toEqual(["command", "cwd", "networkApprovalContext"]);
  const buttons = within(card).getAllByRole("button").map((b) => b.textContent);
  expect(buttons).toEqual(["Approve once", "Reject", "Reject and stop the turn"]);
});

test("a file change is review-and-decline only, with the reason stated", async () => {
  const t = turn({ state: "awaiting_approval", reply: "" });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({
      state: "awaiting_approval",
      active_turn: t.turn_id,
      turns: [t],
      pending_requests: [
        {
          request_id: "9",
          turn_id: t.turn_id,
          kind: "file_change",
          choices: ["reject", "cancel"],
          payload: { changes: [{ path: "a.ts", diff: "+a\n-b" }] },
          complete: false,
        },
      ],
    }),
  );
  renderPane();
  const card = await screen.findByTestId("structured-request");
  expect(within(card).getByTestId("structured-patch")).toHaveTextContent("a.ts");
  expect(within(card).getByTestId("structured-review-only")).toBeInTheDocument();
  expect(within(card).queryByRole("button", { name: /approve/i })).toBeNull();
});

test("a decision shows as in flight, never as success, until the snapshot settles it", async () => {
  const t = turn({ state: "awaiting_approval", reply: "" });
  const pendingSnap = snap({
    state: "awaiting_approval",
    active_turn: t.turn_id,
    turns: [t],
    pending_requests: [
      { request_id: "7", turn_id: t.turn_id, kind: "command", choices: ["approve", "reject"], payload: { command: "ls" } },
    ],
  });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(pendingSnap);
  let release!: () => void;
  vi.mocked(api.structuredDecide).mockImplementation(() => new Promise((r) => (release = () => r({}))));
  renderPane();
  await userEvent.click(await screen.findByRole("button", { name: "Approve once" }));
  expect(screen.getByTestId("structured-deciding")).toHaveTextContent("Sending");
  expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  await act(async () => release());
  // Accepted by the server, but the request is still pending in the snapshot: not settled.
  await waitFor(() => expect(screen.getByTestId("structured-deciding")).toHaveTextContent("waiting for"));
  const [sessionKey, body] = vi.mocked(api.structuredDecide).mock.calls[0];
  expect(sessionKey).toBe(KEY); // the session, never the request's identity
  expect(body).toMatchObject({ request_id: "7", turn_id: t.turn_id, decision: "approve" });
});

test("a decision with no answer keeps its id: retry sends the same decision", async () => {
  const t = turn({ state: "awaiting_approval", reply: "" });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({
      state: "awaiting_approval",
      active_turn: t.turn_id,
      turns: [t],
      pending_requests: [
        { request_id: "7", turn_id: t.turn_id, kind: "command", choices: ["approve", "reject"], payload: { command: "ls" } },
      ],
    }),
  );
  vi.mocked(api.structuredDecide).mockRejectedValueOnce(new TypeError("network")).mockResolvedValueOnce({});
  renderPane();
  await userEvent.click(await screen.findByRole("button", { name: "Reject" }));
  await userEvent.click(await screen.findByRole("button", { name: /Retry “Reject”/ }));
  const [a, b] = vi.mocked(api.structuredDecide).mock.calls.map((c) => c[1]);
  expect(b.decision_id).toBe(a.decision_id);
  expect(b.decision).toBe("reject");
});

test("a stale-revision refusal says why and loses nothing: Edit puts the words back (#1465)", async () => {
  vi.mocked(api.structuredSubmit).mockRejectedValue(new ApiError(409, "the conversation changed (revision 4 → 5)"));
  renderPane();
  const box = await screen.findByRole("textbox");
  await userEvent.type(box, "run the logout spec too");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  const pending = await screen.findByTestId("structured-pending");
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "refused"));
  expect(pending).toHaveTextContent("Not sent: the conversation changed (revision 4 → 5)");
  expect(box).toHaveValue(""); // it left the composer at Send
  expect(within(pending).queryByRole("button", { name: "Retry" })).toBeNull();
  await userEvent.click(within(pending).getByRole("button", { name: "Edit" }));
  expect(box).toHaveValue("run the logout spec too");
  expect(screen.queryByTestId("structured-pending")).toBeNull();
});

test("a send with an unknown outcome offers Retry under the same operation id, never Edit (#1465)", async () => {
  vi.mocked(api.structuredSubmit).mockRejectedValueOnce(new TypeError("network")).mockResolvedValueOnce({});
  renderPane();
  const box = await screen.findByRole("textbox");
  await userEvent.type(box, "hello");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  const pending = await screen.findByTestId("structured-pending");
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "unknown"));
  expect(within(pending).queryByRole("button", { name: "Edit" })).toBeNull();
  expect(within(pending).getByRole("button", { name: "Discard — it may still arrive" })).toBeVisible();
  await userEvent.click(within(pending).getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(api.structuredSubmit).toHaveBeenCalledTimes(2));
  const [a, b] = vi.mocked(api.structuredSubmit).mock.calls;
  expect(b[1]).toBe(a[1]);
  expect(b[2]).toBe("hello");
});

test.each(["codex-api", "claude-api", "opencode-api"])("%s queues two follow-ups without interrupting its active turn (#1378)", async (engine) => {
  const running = turn({ state: "running", reply: "Working" });
  let current = snap({ session_key: `${engine}:${ID}`, state: "running", active_turn: running.turn_id, turns: [running] });
  vi.mocked(api.structuredSnapshot).mockImplementation(async () => current);
  vi.mocked(api.structuredSubmit).mockImplementation(async (_key, id, text) => {
    current = { ...current, revision: current.revision + 1, turns: [...current.turns,
      turn({ turn_id: id, operation_id: id, text, reply: "", state: "queued" })] };
    return { state: "queued" };
  });
  renderPane(engine);
  const box = await screen.findByRole("textbox");
  for (const text of ["next task", "then report"]) {
    await userEvent.type(box, text);
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(box).toHaveValue(""));
  }
  expect(screen.getAllByTestId("structured-queued")).toHaveLength(2);
  expect(screen.getAllByRole("button", { name: "Interrupt" })).toHaveLength(1);
  expect(api.structuredInterrupt).not.toHaveBeenCalled();
  const calls = vi.mocked(api.structuredSubmit).mock.calls;
  expect(calls.map((args) => args[3])).toEqual([undefined, undefined]);
  expect(calls[0][1]).not.toBe(calls[1][1]);
});

test("a lost queued response clears input only when the snapshot proves acceptance (#1378)", async () => {
  const running = turn({ state: "awaiting_approval", reply: "" });
  let current = snap({ state: "awaiting_approval", active_turn: running.turn_id, turns: [running] });
  vi.mocked(api.structuredSnapshot).mockImplementation(async () => current);
  vi.mocked(api.structuredSubmit).mockImplementation(async (_key, id, text) => {
    current = { ...current, revision: 5, turns: [...current.turns,
      turn({ turn_id: id, operation_id: id, text, reply: "", state: "queued" })] };
    throw new TypeError("response lost");
  });
  renderPane();
  const box = await screen.findByRole("textbox");
  await userEvent.type(box, "after approval{enter}");
  await screen.findByTestId("structured-queued");
  expect(box).toHaveValue("");
  expect(api.structuredSubmit).toHaveBeenCalledTimes(1);
  expect(api.structuredInterrupt).not.toHaveBeenCalled();
});

test("a lost connection says the turn keeps running and resumes from the cursor", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const t = turn({ state: "running", reply: "" });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ state: "running", active_turn: t.turn_id, turns: [t] }));
  vi.mocked(api.structuredEvents).mockRejectedValue(new TypeError("offline"));
  renderPane();
  await screen.findByTestId("structured-turn");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_500);
  });
  expect(await screen.findByTestId("structured-reconnecting")).toHaveTextContent("event 4");
  // The head's link LED says it, as a terminal's does for its socket (#1348).
  expect(document.querySelector("[data-head-led]")).toHaveAttribute("data-head-led", "reconnecting");
  expect(api.structuredInterrupt).not.toHaveBeenCalled();
  expect(api.structuredSubmit).not.toHaveBeenCalled();
});

test("a retiring client's history is read only: no send, no decisions", async () => {
  const t = turn({ state: "awaiting_approval", reply: "" });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({
      read_only: "api.source 'codex' is missing, disabled or retiring",
      turns: [t],
      state: "awaiting_approval",
      active_turn: t.turn_id,
      pending_requests: [
        { request_id: "7", turn_id: t.turn_id, kind: "command", choices: ["approve", "reject"], payload: { command: "ls" } },
      ],
    }),
  );
  renderPane();
  expect(await screen.findByTestId("structured-read-only")).toHaveTextContent("missing, disabled");
  expect(screen.getByRole("textbox")).toBeDisabled();
  for (const b of within(screen.getByTestId("structured-request")).getAllByRole("button")) {
    expect(b).toBeDisabled();
  }
  // …but a running worker is never stranded: Stop stays (review of #1311).
  expect(screen.getByRole("button", { name: "Stop the worker" })).toBeEnabled();
});

test("stop reports the proved containment, not just the click", async () => {
  vi.mocked(api.structuredStop).mockResolvedValue({ containment: "gone" });
  renderPane();
  await userEvent.click(await screen.findByRole("button", { name: "Stop the worker" }));
  await waitFor(() => expect(screen.getByTestId("structured-worker")).toHaveTextContent("stopped"));
});

test("a later request that reuses a native request id never inherits an earlier decision", async () => {
  const a = turn({ state: "awaiting_approval", reply: "" });
  const req = (turnId: string, digest: string) => ({
    request_id: "0",
    turn_id: turnId,
    kind: "command",
    choices: ["approve", "reject"],
    payload: { command: "ls" },
    payload_digest: digest,
  });
  let current = snap({ revision: 4, event_cursor: 4, state: "awaiting_approval", active_turn: a.turn_id, turns: [a], pending_requests: [req(a.turn_id, "d1")] });
  vi.mocked(api.structuredSnapshot).mockImplementation(async () => current);
  vi.mocked(api.structuredDecide).mockImplementation(async () => {
    // The first request settles; a NEW turn's first request comes back with the same native id.
    const b = turn({ state: "awaiting_approval", reply: "" });
    current = snap({ revision: 9, event_cursor: 9, state: "awaiting_approval", active_turn: b.turn_id, turns: [{ ...a, state: "completed" }, b], pending_requests: [req(b.turn_id, "d2")] });
    return {};
  });
  renderPane();
  await userEvent.click(await screen.findByRole("button", { name: "Approve once" }));
  await waitFor(() => expect(screen.queryByTestId("structured-deciding")).toBeNull());
  const card = screen.getByTestId("structured-request");
  expect(within(card).getByRole("button", { name: "Approve once" })).toBeEnabled();
  expect(within(card).getByRole("button", { name: "Reject" })).toBeEnabled();
});

test("a retiring client's live worker can still be stopped", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ read_only: "agent removed" }));
  vi.mocked(api.structuredStop).mockResolvedValue({ containment: "gone" });
  renderPane();
  await screen.findByTestId("structured-read-only");
  await userEvent.click(screen.getByRole("button", { name: "Stop the worker" }));
  expect(api.structuredStop).toHaveBeenCalledWith(KEY);
});

test("an older snapshot that arrives late never rolls the view back", async () => {
  const done = turn({ text: "newer", reply: "kept" });
  vi.mocked(api.structuredSnapshot)
    .mockResolvedValueOnce(snap({ revision: 9, event_cursor: 9, turns: [done] }))
    .mockResolvedValue(snap({ revision: 4, event_cursor: 4, turns: [] }));
  vi.mocked(api.structuredStop).mockResolvedValue({ containment: "gone" });
  renderPane();
  expect(await screen.findByText("kept")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Stop the worker" })); // triggers a re-read → rev 4
  await waitFor(() => expect(api.structuredSnapshot).toHaveBeenCalledTimes(2));
  expect(screen.getByText("kept")).toBeInTheDocument();
});

test("a worker that exits without a journal record is noticed by the periodic re-read", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.structuredSnapshot)
    .mockResolvedValueOnce(snap())
    .mockResolvedValue(snap({ native: { native_id: "n", worker: null, background_active: false } }));
  renderPane();
  await waitFor(() => expect(screen.getByTestId("structured-worker")).toHaveTextContent("worker live"));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_500); // one idle tick; the journal revision is unchanged
  });
  await waitFor(() => expect(screen.getByTestId("structured-worker")).toHaveTextContent("no worker"));
});

test.each([false, true])("a permission change disables approval controls and open grants (unknown decision: %s)", async (unknown) => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const t = turn({ state: "awaiting_approval", reply: "" });
  const before = snap({
    state: "awaiting_approval", active_turn: t.turn_id, turns: [t], can_enable_bypass: true,
    pending_requests: [{
      request_id: "7", turn_id: t.turn_id, kind: "command", complete: true,
      choices: ["approve", "reject"], payload: { command: "ls" },
      // Two of one scope, so the list opens (otherwise each grant is its own button).
      always: [
        { id: "g0123456789abcdef", label: "Always, saved to this project", scope: "persistent" },
        { id: "gfedcba9876543210", label: "Always, saved to every project", scope: "persistent" },
      ],
    }],
  });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(before);
  vi.mocked(api.structuredDecide).mockRejectedValue(new TypeError("connection lost"));
  renderPane();
  const card = await screen.findByTestId("structured-request");
  if (unknown) {
    await user.click(within(card).getByRole("button", { name: "Approve once" }));
    await screen.findByRole("button", { name: /Retry “Approve once”/ });
  } else {
    await user.click(within(card).getByTestId("structured-always"));
    expect(within(card).getAllByTestId("structured-always-option")[0]).toBeEnabled();
  }
  vi.mocked(api.structuredSnapshot).mockResolvedValue({
    ...before, permission_change: { state: "stopping", operation_id: crypto.randomUUID() },
  });
  await act(async () => { await vi.advanceTimersByTimeAsync(10_500); });
  await waitFor(() => {
    for (const button of within(card).getAllByRole("button")) expect(button).toBeDisabled();
  });
  for (const button of within(card).getAllByRole("button")) await user.click(button);
  expect(api.structuredDecide).toHaveBeenCalledTimes(unknown ? 1 : 0);
  expect(api.structuredInterrupt).not.toHaveBeenCalled();
});

test("an incomplete tool request explains the missing arguments before Details", async () => {
  const t = turn({ state: "awaiting_approval", reply: "" });
  const reason = "OpenCode did not include this tool’s arguments, so BattleLab cannot show what would be approved. Deny the request or stop the turn.";
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({
    state: "awaiting_approval", active_turn: t.turn_id, turns: [t],
    pending_requests: [{
      request_id: "mcp", turn_id: t.turn_id, kind: "other", complete: false,
      choices: ["reject"], payload: { toolCall: { title: "forgejo_list_issues", rawInput: {} }, declineOnly: reason },
    }],
  }));
  renderPane("opencode-api");
  const card = await screen.findByTestId("structured-request");
  const reasons = within(card).getAllByText(reason);
  expect(reasons.some((node) => !node.closest("details"))).toBe(true);
  expect(within(card).queryByRole("button", { name: /Allow/ })).toBeNull();
  expect(within(card).getByRole("button", { name: "Deny" })).toBeEnabled();
});

test("a delayed pre-enable snapshot cannot restore closed approval buttons at the same revision", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const t = turn({ state: "interrupted", reply: "kept history" });
  const enabled = snap({
    state: "interrupted",
    turns: [t],
    bypass: true,
    can_enable_bypass: false,
    permission_change: { state: "enabled", operation_id: crypto.randomUUID() },
    native: { native_id: "n", worker: null, background_active: false },
  });
  const before = snap({
    state: "awaiting_approval",
    active_turn: t.turn_id,
    turns: [{ ...t, state: "awaiting_approval" }],
    bypass: false,
    can_enable_bypass: true,
    pending_requests: [{
      request_id: "7", turn_id: t.turn_id, kind: "command",
      choices: ["approve", "reject"], payload: { command: "ls" }, complete: true,
    }],
  });
  vi.mocked(api.structuredSnapshot).mockResolvedValueOnce(enabled).mockResolvedValue(before);
  renderPane();
  await screen.findByTestId("structured-bypass");
  await act(async () => { await vi.advanceTimersByTimeAsync(10_500); });
  expect(api.structuredSnapshot).toHaveBeenCalledTimes(2);
  expect(screen.queryByTestId("structured-request")).toBeNull();
  expect(screen.getByTestId("structured-bypass")).toBeInTheDocument();
  expect(screen.getByText("kept history")).toBeInTheDocument();
});

// ---- the pane head (#1332) ---------------------------------------------------------------------

test("the pane wears the terminal's bar and composer, and opens on an info screen (#1348)", async () => {
  clearSent();
  renderPane();
  const info = await screen.findByRole("region", { name: "Session info" });
  // The facts, not the old "No terminal: …" paragraph.
  expect(screen.queryByText(/no terminal/i)).toBeNull();
  expect(within(info).getByText("Model").nextElementSibling).toHaveTextContent("gpt-5-codex");
  expect(within(info).getByText("Folder").nextElementSibling).toHaveTextContent("/w");
  expect(within(info).getByText("Worker").nextElementSibling).toHaveTextContent("worker live");
  // The terminal's own bar: the shared facts run (LED + engine box) and its actions.
  const bar = document.querySelector("[data-panel-head]") as HTMLElement;
  expect(bar).not.toBeNull();
  expect(bar.querySelector("[data-head-led]")).toHaveAttribute("data-head-led", "live");
  expect(within(bar).getByRole("button", { name: "Stop the worker" })).toBeInTheDocument();
  // The terminal's composer: one row of chips (KeyBar), then push-to-talk's slot and Send.
  const form = screen.getByRole("form", { name: "Compose message" });
  const chips = Array.from(form.querySelectorAll("[data-key]")).map((b) => b.getAttribute("aria-label"));
  expect(chips).toEqual(["Use a template"]);
  expect(within(form).getByRole("button", { name: "Send" })).toBeDisabled();
});

test("an agent-default session's info names the model and level its client reported (#1511)", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({
      model_requested: null,
      model_effective: null,
      model_configured: "gpt-6-codex",
      effort_configured: "high",
    }),
  );
  renderPane();
  const model = await screen.findByTestId("structured-model");
  await waitFor(() => expect(model).toHaveTextContent("gpt-6-codex · high · agent default"));
});

test("before the client reports, an agent-default session still says so (#1511)", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({ model_requested: null, model_effective: null }),
  );
  renderPane();
  const model = await screen.findByTestId("structured-model");
  await waitFor(() => expect(model).toHaveTextContent(/^agent default$/));
});

test("a conversation with turns shows no info screen", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ turns: [turn({ reply: "hi" })] }));
  renderPane();
  await screen.findByTestId("structured-turn");
  expect(screen.queryByRole("region", { name: "Session info" })).toBeNull();
});

function renderHosted(host: Parameters<typeof StructuredPane>[0]["host"]) {
  return render(
    <MemoryRouter>
      <StructuredPane engine="codex-api" id={ID} host={host} />
    </MemoryRouter>,
  );
}

test("the head carries the terminal's actions that work for an API session — never Adopt to mission", async () => {
  const onToggleFiles = vi.fn();
  const onToMap = vi.fn();
  renderHosted({ onToggleFiles, onToMap, filesOpen: false });
  await screen.findByTestId("structured-worker");
  const names = screen
    .getAllByRole("button")
    .map((b) => b.getAttribute("aria-label"))
    .filter(Boolean);
  expect(names).toEqual(
    expect.arrayContaining([
      "Browse session files",
      "Open session brief",
      "Hand off session to another engine",
      "Open this session as a window on the map",
      "Share a link to this session",
    ]),
  );
  // Ordered as the terminal's head is.
  const ids = Array.from(document.querySelectorAll("[data-head-action]")).map((b) =>
    b.getAttribute("aria-label"),
  );
  expect(ids).toEqual([
    "Browse session files",
    "Open session brief",
    "Hand off session to another engine",
    "Open this session as a window on the map",
    "Share a link to this session",
    "Share this session publicly",
    "Stop the worker",
  ]);
  expect(screen.queryByRole("button", { name: /adopt this session/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /repaint/i })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Browse session files" }));
  expect(onToggleFiles).toHaveBeenCalledTimes(1);
  await userEvent.click(screen.getByRole("button", { name: "Open this session as a window on the map" }));
  expect(onToMap).toHaveBeenCalledTimes(1);
});

test("Files is a visible, disabled trigger until the session reports a folder; no host = no Files", async () => {
  const { unmount } = renderHosted({
    onToggleFiles: vi.fn(),
    filesDisabledReason: "This session has not reported a folder yet",
  });
  expect(await screen.findByRole("button", { name: "Browse session files" })).toBeDisabled();
  unmount();
  renderPane();
  await screen.findByTestId("structured-worker");
  expect(screen.queryByRole("button", { name: "Browse session files" })).toBeNull();
  expect(screen.getByRole("button", { name: "Open session brief" })).toBeInTheDocument();
});

test("Recap opens the session brief for this session", async () => {
  renderHosted({});
  await userEvent.click(await screen.findByRole("button", { name: "Open session brief" }));
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
});

test("at ≤800px every action lives in ONE Actions menu", async () => {
  const original = window.matchMedia;
  window.matchMedia = ((q: string) =>
    ({
      matches: q.includes("max-width: 800px"),
      media: q,
      addEventListener: () => {},
      removeEventListener: () => {},
    }) as unknown as MediaQueryList) as typeof window.matchMedia;
  try {
    renderHosted({ onToggleFiles: vi.fn() });
    await screen.findByTestId("structured-worker");
    expect(document.querySelectorAll("[data-head-action]")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: /actions/i }));
    const items = screen.getAllByRole("menuitem").map((m) => m.textContent);
    expect(items).toEqual(["Files", "Recap", "Hand off", "Share link", "Share publicly…", "Stop"]);
  } finally {
    window.matchMedia = original;
  }
});

test("in a map window the pane has no bar: its actions portal into the chrome slot, Stop with them", async () => {
  const slot = document.createElement("span");
  document.body.appendChild(slot);
  const onTermStatus = vi.fn();
  vi.mocked(api.structuredStop).mockResolvedValue({ containment: "gone" });
  try {
    renderHosted({
      suppressHead: true,
      headActionsSlot: slot,
      headOverflowRef: { current: [] },
      onTermStatus,
      onToggleFiles: vi.fn(),
    });
    await waitFor(() => expect(onTermStatus).toHaveBeenLastCalledWith({ kind: "connected" }));
    expect(document.querySelector("[data-panel-head]")).toBeNull(); // no second bar
    const chips = within(slot);
    expect(chips.getByRole("button", { name: "Browse session files" })).toBeInTheDocument();
    await userEvent.click(chips.getByRole("button", { name: "Stop the worker" }));
    expect(api.structuredStop).toHaveBeenCalledWith(KEY);
  } finally {
    slot.remove();
  }
});

// ---- pictures in a send (#1332 Phase 3) --------------------------------------------------------

function png(name = "shot.png") {
  return new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], name, { type: "image/png" });
}

function uploaded(n: number) {
  vi.mocked(api.upload).mockResolvedValueOnce({
    path: `/h/.agent-sessions/uploads/20261008-01000${n}-shot.png`,
    name: "shot.png",
    stored: `20261008-01000${n}-shot.png`,
  });
}

test("a document-only message uploads and sends for a file-capable client without native images", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ files: true, images: false }));
  vi.mocked(api.structuredSubmit).mockResolvedValue({});
  vi.mocked(api.upload).mockResolvedValue({
    name: "brief.pdf", stored: "20261010-010000-brief.pdf", path: "/h/.agent-sessions/uploads/20261010-010000-brief.pdf",
  });
  renderPane();
  await screen.findByRole("button", { name: "Attach files" });
  const file = new File(["%PDF-1.4 contents"], "brief.pdf", { type: "application/pdf" });
  await userEvent.upload(screen.getByTestId("structured-file-input"), file);
  await screen.findByTestId("structured-attachment");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(api.upload).toHaveBeenCalledWith(file);
  expect(vi.mocked(api.structuredSubmit).mock.calls[0].slice(2)).toEqual(["", 4, ["20261010-010000-brief.pdf"]]);
});

test("pasted documents with an empty browser MIME type become attachments", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ files: true, images: false }));
  vi.mocked(api.upload).mockResolvedValue({
    name: "LICENSE", stored: "20261010-010000-LICENSE", path: "/h/.agent-sessions/uploads/20261010-010000-LICENSE",
  });
  renderPane();
  const box = await screen.findByRole("textbox");
  const file = new File(["license text"], "LICENSE");
  box.focus();
  await userEvent.paste({ files: [file], items: [{ kind: "file", type: "", getAsFile: () => file }], getData: () => "" } as unknown as DataTransfer);
  expect(await screen.findByTestId("structured-attachment")).toHaveTextContent("LICENSE");
  expect(api.upload).toHaveBeenCalledWith(file);
});

test("a client that takes no pictures offers no attach", async () => {
  renderPane();
  await screen.findByRole("textbox");
  expect(screen.queryByRole("button", { name: "Attach images" })).toBeNull();
});

test("attached pictures ride the send by upload name; an edited resend is a new operation", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  vi.mocked(api.structuredSubmit).mockRejectedValueOnce(new ApiError(422, "a picture could not be read")).mockResolvedValue({});
  uploaded(1);
  uploaded(2);
  renderPane();
  await screen.findByRole("button", { name: "Attach images" });
  await userEvent.upload(screen.getByTestId("structured-file-input"), [png("a.png"), png("b.png")]);
  expect(await screen.findAllByTestId("structured-attachment")).toHaveLength(2);
  await userEvent.type(screen.getByRole("textbox"), "what is wrong here");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(screen.queryAllByTestId("structured-attachment")).toHaveLength(0); // left with the message
  const pending = await screen.findByTestId("structured-pending");
  expect(within(pending).getAllByTestId("structured-pending-attachment")).toHaveLength(2);
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "refused"));
  await userEvent.click(within(pending).getByRole("button", { name: "Edit" })); // pictures come back
  expect(screen.getAllByTestId("structured-attachment")).toHaveLength(2);
  await userEvent.click(screen.getAllByRole("button", { name: "Remove shot.png" })[1]);
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  const [first, second] = vi.mocked(api.structuredSubmit).mock.calls;
  expect(first[2]).toBe("what is wrong here");
  expect(first[4]).toEqual(["20261008-010001-shot.png", "20261008-010002-shot.png"]);
  expect(second[4]).toEqual(["20261008-010001-shot.png"]);
  expect(second[1]).not.toBe(first[1]); // different pictures = a different request
  await waitFor(() => expect(screen.queryAllByTestId("structured-attachment")).toHaveLength(0));
});

test("a picture alone can be sent; a pasted image becomes an attachment", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  vi.mocked(api.structuredSubmit).mockResolvedValue({});
  uploaded(3);
  renderPane();
  const box = await screen.findByRole("textbox");
  expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  box.focus();
  await userEvent.paste({
    files: [png()],
    items: [{ kind: "file", type: "image/png", getAsFile: () => png() }],
    getData: () => "",
  } as unknown as DataTransfer);
  await screen.findByTestId("structured-attachment");
  const sendBtn = screen.getByRole("button", { name: "Send" });
  await waitFor(() => expect(sendBtn).toBeEnabled());
  await userEvent.click(sendBtn);
  const [call] = vi.mocked(api.structuredSubmit).mock.calls;
  expect(call[2]).toBe("");
  expect(call[4]).toEqual(["20261008-010003-shot.png"]);
});

test("a fifth picture is refused before upload, with the cap stated", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  for (let i = 1; i <= 4; i++) uploaded(i);
  renderPane();
  await screen.findByRole("button", { name: "Attach images" });
  await userEvent.upload(
    screen.getByTestId("structured-file-input"),
    [1, 2, 3, 4, 5].map((i) => png(`${i}.png`)),
  );
  expect(await screen.findByTestId("structured-error")).toHaveTextContent("at most 4 images");
  await waitFor(() => expect(screen.getAllByTestId("structured-attachment")).toHaveLength(4));
  expect(api.upload).toHaveBeenCalledTimes(4);
  expect(screen.getByRole("button", { name: "Attach images" })).toBeDisabled();
});

function deferredUpload() {
  let resolve!: (n: number) => void;
  const p = new Promise<number>((r) => (resolve = r));
  vi.mocked(api.upload).mockImplementationOnce(async () => {
    const n = await p;
    return {
      path: `/h/.agent-sessions/uploads/20261008-01000${n}-shot.png`,
      name: `shot${n}.png`,
      stored: `20261008-01000${n}-shot.png`,
    };
  });
  return resolve;
}

test("overlapping uploads hold the send until EVERY picture has landed (Hermes on #1345)", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  vi.mocked(api.structuredSubmit).mockResolvedValue({});
  const first = deferredUpload();
  const second = deferredUpload();
  renderPane();
  const input = await screen.findByTestId("structured-file-input");
  await userEvent.type(screen.getByRole("textbox"), "compare these");
  await userEvent.upload(input, [png("a.png")]);
  await userEvent.upload(input, [png("b.png")]);
  const sendBtn = screen.getByRole("button", { name: /Send|Uploading/ });
  await act(async () => first(1));
  await screen.findByTestId("structured-attachment");
  expect(sendBtn).toBeDisabled(); // the second picture is still in flight
  await userEvent.keyboard("{Enter}"); // Enter takes the same guard as the button
  expect(api.structuredSubmit).not.toHaveBeenCalled();
  await act(async () => second(2));
  await waitFor(() => expect(screen.getAllByTestId("structured-attachment")).toHaveLength(2));
  await waitFor(() => expect(sendBtn).toBeEnabled());
  await userEvent.click(sendBtn);
  const [call] = vi.mocked(api.structuredSubmit).mock.calls;
  expect(call[4]).toEqual(["20261008-010001-shot.png", "20261008-010002-shot.png"]);
});

test("overlapping batches never exceed the cap: in-flight pictures hold their slots", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  const releases = [1, 2, 3, 4].map(() => deferredUpload());
  renderPane();
  const input = await screen.findByTestId("structured-file-input");
  await userEvent.upload(input, [png("1.png"), png("2.png"), png("3.png")]);
  await userEvent.upload(input, [png("4.png"), png("5.png"), png("6.png")]);
  expect(await screen.findByTestId("structured-error")).toHaveTextContent("at most 4 images");
  await act(async () => releases.forEach((r, i) => r(i + 1)));
  await waitFor(() => expect(screen.getAllByTestId("structured-attachment")).toHaveLength(4));
  expect(api.upload).toHaveBeenCalledTimes(4);
});

/** A paste whose DataTransfer yields no usable file — the deferred-clipboard shape (#530). */
const deferredPaste = {
  files: [],
  items: [{ kind: "file", type: "", getAsFile: () => null }],
  getData: () => "",
} as unknown as DataTransfer;

test("a deferred clipboard read holds the send until its picture lands (Hermes on #1345, round 2)", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  vi.mocked(api.structuredSubmit).mockResolvedValue({});
  let read!: (files: File[]) => void;
  vi.mocked(imageFilesFromAsyncClipboard).mockImplementationOnce(
    () => new Promise<File[]>((r) => (read = r)),
  );
  uploaded(5);
  renderPane();
  const box = await screen.findByRole("textbox");
  await userEvent.type(box, "see this");
  box.focus();
  await userEvent.paste(deferredPaste);
  const sendBtn = screen.getByRole("button", { name: /Send|Uploading/ });
  expect(sendBtn).toBeDisabled(); // the clipboard is still being read
  await userEvent.keyboard("{Enter}");
  expect(api.structuredSubmit).not.toHaveBeenCalled();
  await act(async () => read([png()]));
  await screen.findByTestId("structured-attachment");
  await waitFor(() => expect(sendBtn).toBeEnabled());
  await userEvent.click(sendBtn);
  expect(vi.mocked(api.structuredSubmit).mock.calls[0][4]).toEqual(["20261008-010005-shot.png"]);
});

test("a paste while a message is sending joins the NEXT message, never the one on its way (#1465)", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  let post!: () => void;
  vi.mocked(api.structuredSubmit).mockImplementationOnce(
    () => new Promise((r) => (post = () => r({}))),
  );
  uploaded(6);
  uploaded(8);
  renderPane();
  const box = await screen.findByRole("textbox");
  await userEvent.upload(screen.getByTestId("structured-file-input"), [png()]);
  await screen.findByTestId("structured-attachment");
  await userEvent.type(box, "first");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  box.focus();
  await userEvent.paste({
    files: [png("late.png")],
    items: [{ kind: "file", type: "image/png", getAsFile: () => png("late.png") }],
    getData: () => "",
  } as unknown as DataTransfer);
  expect(await screen.findByTestId("structured-attachment")).toBeVisible(); // in the composer
  await act(async () => post());
  expect(vi.mocked(api.structuredSubmit).mock.calls[0][4]).toEqual(["20261008-010006-shot.png"]);
  expect(screen.getAllByTestId("structured-attachment")).toHaveLength(1); // still the next message's
});

// ---- sent history and templates (#1332 Phase 3b) ------------------------------------------------

function template(over: Partial<Template> = {}): Template {
  return {
    id: "repro",
    name: "Repro steps",
    description: "",
    tags: [],
    body: "Reproduce {{what}} in a real browser",
    fields: [{ name: "what", label: "What", default: "", required: true }],
    images: [{ name: "shot.png", path: "/u/.agent-sessions/uploads/20261008-020000-shot.png" }],
    created_at: 1,
    updated_at: 1,
    used_count: 0,
    last_used_at: null,
    ...over,
  };
}

test("a send is recorded once per operation, confirmed only when the server has it, and Restore refills it", async () => {
  clearSent();
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  vi.mocked(api.structuredSubmit).mockRejectedValueOnce(new TypeError("network")).mockResolvedValueOnce({});
  uploaded(7);
  renderPane();
  await userEvent.upload(await screen.findByTestId("structured-file-input"), [png()]);
  await screen.findByTestId("structured-attachment");
  await userEvent.type(screen.getByRole("textbox"), "look at this  ");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  const pending = await screen.findByTestId("structured-pending");
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "unknown"));
  const [entry] = readSent();
  expect(readSent()).toHaveLength(1);
  expect(entry).toMatchObject({
    text: "look at this  ", // untrimmed, so Restore round-trips it
    attachments: ["20261008-010007-shot.png"],
    session: KEY,
    confirmed: false,
  });
  await userEvent.click(within(pending).getByRole("button", { name: "Retry" })); // the safe retry
  await waitFor(() => expect(readSent()[0].confirmed).toBe(true));
  expect(readSent()).toHaveLength(1); // the same operation: no second entry
  expect(vi.mocked(api.structuredSubmit).mock.calls[1][1]).toBe(vi.mocked(api.structuredSubmit).mock.calls[0][1]);
  await userEvent.click(screen.getByRole("button", { name: "Sent messages" }));
  await userEvent.click(await screen.findByRole("button", { name: /restore/i }));
  expect(screen.getByRole("textbox")).toHaveValue("look at this  ");
  expect(screen.getAllByTestId("structured-attachment")).toHaveLength(1);
});

test("Restore into a client that takes no pictures keeps the words and says what was left out", async () => {
  clearSent();
  localStorage.setItem(
    "as:sent:v1",
    JSON.stringify([
      { id: "a", text: "from the terminal", attachments: ["/u/x/20261008-1-shot.png"], ts: 1, confirmed: true, session: "claude:s" },
    ]),
  );
  renderPane();
  await userEvent.click(await screen.findByRole("button", { name: "Sent messages" }));
  await userEvent.click(await screen.findByRole("button", { name: /restore/i }));
  expect(screen.getByRole("textbox")).toHaveValue("from the terminal");
  expect(screen.queryAllByTestId("structured-attachment")).toHaveLength(0);
  expect(screen.getByTestId("structured-error")).toHaveTextContent("Codex takes no images");
});

test("a template inserts its filled text and its pictures; a secret template cannot be inserted", async () => {
  clearSent();
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  vi.mocked(api.templates).mockResolvedValue({
    templates: [
      template(),
      template({
        id: "deploy",
        name: "Deploy",
        body: "token {{t}}",
        images: [],
        fields: [{ name: "t", label: "Token", default: "", required: true, source: "template", kind: "secret" }],
      }),
    ],
  } as never);
  renderPane();
  await userEvent.click(await screen.findByRole("button", { name: "Use a template" }));
  await userEvent.click(await screen.findByText("Deploy"));
  expect(screen.getByRole("button", { name: "Insert Deploy into message" })).toBeDisabled();
  await userEvent.click(screen.getByText("Repro steps"));
  await userEvent.type(screen.getByRole("textbox", { name: /^What/ }), "the logout bug");
  await userEvent.click(screen.getByRole("button", { name: "Insert Repro steps into message" }));
  expect(screen.getByRole("textbox")).toHaveValue("Reproduce the logout bug in a real browser");
  expect(screen.getAllByTestId("structured-attachment")).toHaveLength(1);
  expect(api.structuredSubmit).not.toHaveBeenCalled(); // Insert never sends
});


test("Sent appears when the shared ring gets its first entry after mount — this tab or another", async () => {
  clearSent();
  renderPane();
  await screen.findByRole("textbox");
  expect(screen.queryByRole("button", { name: "Sent messages" })).toBeNull();
  // Another composer in this tab (a terminal, a map window) sends.
  act(() => {
    appendSent({ text: "from the terminal", attachments: [], session: "claude:s" });
  });
  expect(await screen.findByRole("button", { name: "Sent messages" })).toBeVisible();
  clearSent();
  // Another TAB writes: only a `storage` event arrives.
  const entry = { id: "x", text: "other tab", attachments: [], ts: 1, confirmed: true, session: null };
  localStorage.setItem("as:sent:v1", JSON.stringify([entry]));
  act(() => {
    window.dispatchEvent(new StorageEvent("storage", { key: "as:sent:v1" }));
  });
  await userEvent.click(await screen.findByRole("button", { name: "Sent messages" }));
  expect(await screen.findByText("other tab")).toBeVisible();
});


test("an unknown outcome is settled by the conversation: its operation appears as a turn", async () => {
  clearSent();
  const op = "77777777-7777-4777-8777-777777777777";
  appendSent({ text: "deploy it", attachments: [], session: KEY, operation: op });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ turns: [turn({ turn_id: op, operation_id: op })] }));
  renderPane();
  await waitFor(() => expect(readSent()[0].confirmed).toBe(true));
});

test("Restoring a CONFIRMED message and sending it again is a new turn, never a replay of the old one", async () => {
  clearSent();
  const op = "88888888-8888-4888-8888-888888888888";
  appendSent({ text: "run the suite", attachments: [], session: KEY, operation: op });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ turns: [turn({ turn_id: op, operation_id: op })] }));
  vi.mocked(api.structuredSubmit).mockResolvedValue({});
  renderPane();
  await waitFor(() => expect(readSent()[0].confirmed).toBe(true));
  await userEvent.click(await screen.findByRole("button", { name: "Sent messages" }));
  await userEvent.click(await screen.findByRole("button", { name: /restore/i }));
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  const [call] = vi.mocked(api.structuredSubmit).mock.calls;
  expect(call[1]).not.toBe(op);
  expect(call[2]).toBe("run the suite");
  expect(readSent()).toHaveLength(2); // a new send, recorded as one
});

test("Templates and Sent wait while a picture uploads: a restore never abandons work in flight", async () => {
  clearSent();
  appendSent({ text: "older words", attachments: [], session: KEY });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  const late = deferredUpload();
  renderPane();
  await userEvent.upload(await screen.findByTestId("structured-file-input"), [png()]);
  expect(screen.getByRole("button", { name: "Sent messages" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Use a template" })).toBeDisabled();
  await act(async () => late(9));
  await screen.findByTestId("structured-attachment");
  expect(screen.getByRole("button", { name: "Sent messages" })).toBeEnabled();
});

test("committed, answer and re-read lost, reload: Restore + Send re-sends under the SAME id — never a second turn", async () => {
  clearSent();
  let reads = 0;
  vi.mocked(api.structuredSnapshot).mockImplementation(async () => {
    reads += 1;
    if (reads > 1) throw new TypeError("network");
    return snap();
  });
  vi.mocked(api.structuredSubmit).mockRejectedValueOnce(new TypeError("network"));
  const first = renderPane();
  await userEvent.type(await screen.findByRole("textbox"), "deploy it");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByTestId("structured-pending")).toHaveAttribute("data-phase", "unknown"));
  const lostOp = vi.mocked(api.structuredSubmit).mock.calls[0][1];
  expect(readSent()[0]).toMatchObject({ operation: lostOp, confirmed: false });
  first.unmount(); // a reload

  vi.mocked(api.structuredSnapshot).mockReset().mockResolvedValue(snap());
  vi.mocked(api.structuredSubmit).mockResolvedValueOnce({});
  renderPane();
  await userEvent.click(await screen.findByRole("button", { name: "Sent messages" }));
  expect(await screen.findByText("Outcome unknown")).toBeVisible();
  expect(screen.queryByText("Unconfirmed")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: /restore/i }));
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(vi.mocked(api.structuredSubmit).mock.calls[1][1]).toBe(lostOp); // the server replays it
  expect(readSent()).toHaveLength(1);
});

test("an unknown send the conversation shows as recorded is settled by it: a repeat is a new turn (Hermes on #1346)", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let reads = 0;
  let landed: string | null = null;
  vi.mocked(api.structuredSnapshot).mockImplementation(async () => {
    reads += 1;
    if (reads === 2) throw new TypeError("network"); // the re-read right after the lost answer
    return landed ? snap({ turns: [turn({ turn_id: landed, operation_id: landed, text: "deploy it" })] }) : snap();
  });
  vi.mocked(api.structuredSubmit).mockImplementationOnce(async (_k, op) => {
    landed = op; // the server recorded it — only the answer is lost
    throw new TypeError("network");
  });
  vi.mocked(api.structuredSubmit).mockResolvedValue({});
  renderPane();
  const box = await screen.findByRole("textbox");
  await userEvent.type(box, "deploy it");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByTestId("structured-pending")).toHaveAttribute("data-phase", "unknown"));
  expect(box).toHaveValue(""); // left the composer at Send
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15_000); // the periodic re-read finds the turn
  });
  // It WAS sent: the conversation's turn replaces the entry, once.
  await waitFor(() => expect(screen.queryByTestId("structured-pending")).toBeNull());
  expect(screen.getAllByTestId("structured-turn")).toHaveLength(1);
  await userEvent.type(box, "deploy it");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  const calls = vi.mocked(api.structuredSubmit).mock.calls;
  expect(calls[calls.length - 1][1]).not.toBe(landed); // a repeat is a new turn
});

test.each([
  [true, 1],
  [false, 0],
])("in a map window (head suppressed) a skip session still says so: bypass=%s (Hermes 5948)", async (bypass, shown) => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ bypass }));
  renderHosted({ suppressHead: true });
  await screen.findByTestId("structured-worker");
  expect(screen.queryAllByTestId("structured-bypass-banner")).toHaveLength(shown);
  if (shown) {
    expect(screen.getByTestId("structured-bypass-banner")).toHaveTextContent(/skip permissions/i);
  }
});

// ---- #1339: approve always + risk marks -----------------------------------------------------------

function pendingWith(over: Partial<StructuredRequest>) {
  const t = turn({ state: "awaiting_approval", reply: "" });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({
      state: "awaiting_approval",
      active_turn: t.turn_id,
      turns: [t],
      pending_requests: [
        {
          request_id: "7",
          turn_id: t.turn_id,
          kind: "command",
          choices: ["approve", "reject", "cancel"],
          payload: { command: "cat shared/agent-workflow.md" },
          payload_digest: "abcdef0123",
          complete: true,
          ...over,
        },
      ],
    }),
  );
  return t;
}

const PERSIST = {
  id: "g0123456789abcdef",
  scope: "persistent",
  label:
    "Always allow `cat shared/agent-workflow.md` and any command starting with it · saved to Codex's exec policy (persists)",
};
const SESSION = { id: "gfedcba9876543210", scope: "session", label: "Allow `cat x` for the rest of this session" };

test("a session grant and a persistent one are each one click, labelled by how long they last", async () => {
  const t = pendingWith({ always: [PERSIST, SESSION], risk: { level: "none", reasons: [] } });
  vi.mocked(api.structuredDecide).mockResolvedValue({});
  const user = userEvent.setup();
  renderPane();
  const card = await screen.findByTestId("structured-request");
  expect(within(card).queryByTestId("structured-risk")).toBeNull(); // "none" is never labelled safe
  expect(within(card).getAllByRole("button").map((b) => b.textContent)).toEqual([
    "Approve once",
    "Approve for session",
    "Approve always",
    "Reject",
    "Reject and stop the turn",
  ]);
  const [session, persist] = within(card).getAllByTestId("structured-always");
  expect(session).toHaveAttribute("title", SESSION.label);
  expect(persist).toHaveAttribute("title", PERSIST.label);
  expect(persist).not.toHaveAttribute("aria-expanded");
  // The full labels stay reachable without hover, in Details.
  expect(within(card).getByText(/and any command starting with it/)).toBeInTheDocument();
  await user.click(persist);
  expect(within(card).queryByTestId("structured-always-option")).toBeNull();
  await vi.waitFor(() => expect(api.structuredDecide).toHaveBeenCalledTimes(1));
  const [, body] = vi.mocked(api.structuredDecide).mock.calls[0];
  expect(body).toMatchObject({ turn_id: t.turn_id, request_id: "7", decision: "always", grant: PERSIST.id });
});

test("two grants of one scope still open the list and send the chosen id (#1339)", async () => {
  const project = { ...PERSIST, id: "g1111111111111111", label: "Always allow `cat` · saved to this project" };
  const t = pendingWith({ always: [PERSIST, project] });
  vi.mocked(api.structuredDecide).mockResolvedValue({});
  const user = userEvent.setup();
  renderPane();
  const card = await screen.findByTestId("structured-request");
  const toggle = within(card).getByTestId("structured-always");
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  await user.click(toggle);
  const options = within(card).getAllByTestId("structured-always-option");
  expect(options.map((o) => o.textContent)).toEqual([PERSIST.label, project.label]);
  await user.click(options[1]);
  await vi.waitFor(() => expect(api.structuredDecide).toHaveBeenCalledTimes(1));
  const [, body] = vi.mocked(api.structuredDecide).mock.calls[0];
  expect(body).toMatchObject({ turn_id: t.turn_id, request_id: "7", decision: "always", grant: project.id });
});

test("one proposed grant: Approve always sends it in one click, no list to pick from", async () => {
  const t = pendingWith({ always: [PERSIST] });
  vi.mocked(api.structuredDecide).mockResolvedValue({});
  const user = userEvent.setup();
  renderPane();
  const card = await screen.findByTestId("structured-request");
  const always = within(card).getByTestId("structured-always");
  expect(always).toHaveTextContent(/^Approve always$/);
  expect(always).not.toHaveAttribute("aria-expanded");
  expect(always).toHaveAttribute("title", PERSIST.label);
  await user.click(always);
  expect(within(card).queryByTestId("structured-always-option")).toBeNull();
  await vi.waitFor(() => expect(api.structuredDecide).toHaveBeenCalledTimes(1));
  const [, body] = vi.mocked(api.structuredDecide).mock.calls[0];
  expect(body).toMatchObject({ turn_id: t.turn_id, request_id: "7", decision: "always", grant: PERSIST.id });
});

test("no always grants: no Approve always control at all (#1339)", async () => {
  pendingWith({ always: [] });
  renderPane();
  const card = await screen.findByTestId("structured-request");
  expect(within(card).queryByTestId("structured-always")).toBeNull();
  expect(within(card).getAllByRole("button").map((b) => b.textContent)).toEqual([
    "Approve once",
    "Reject",
    "Reject and stop the turn",
  ]);
});

test("a risky command without a proposed grant shows its reasons and no Approve always (#1339)", async () => {
  pendingWith({
    payload: { command: "git push --force origin main" },
    always: [],
    risk: { level: "risky", reasons: ["force-pushes over remote history"] },
  });
  renderPane();
  const card = await screen.findByTestId("structured-request");
  const mark = within(card).getByTestId("structured-risk");
  expect(mark).toHaveAttribute("data-level", "risky");
  expect(mark).toHaveTextContent(/risky/i);
  expect(mark).toHaveTextContent("force-pushes over remote history");
  expect(within(card).queryByTestId("structured-always")).toBeNull();
});

test("an unreadable command is 'not classified', never safe; tool rows carry the same mark (#1339)", async () => {
  const t = turn({
    state: "completed",
    tools: [
      { id: "a", name: "commandExecution", outcome: "completed", summary: "rm -rf dist", risk: { level: "risky", reasons: ["deletes files recursively or forcibly"] } },
      { id: "b", name: "Bash", outcome: "completed", summary: "{}", risk: { level: "unknown", reasons: [] } },
      { id: "c", name: "commandExecution", outcome: "completed", summary: "npm test", risk: { level: "none", reasons: [] } },
    ],
  });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ turns: [t] }));
  renderPane();
  const rows = await screen.findAllByTestId("structured-tool");
  expect(within(rows[0]).getByTestId("structured-risk")).toHaveTextContent("deletes files recursively");
  expect(within(rows[1]).getByTestId("structured-risk")).toHaveTextContent("not classified");
  expect(within(rows[2]).queryByTestId("structured-risk")).toBeNull();
  expect(document.body).not.toHaveTextContent(/\bsafe\b/i);
});

test("the suggestions hint matches a broad offered rule — it never claims only bounded ones (Hermes 5919)", async () => {
  pendingWith({
    kind: "tool",
    payload: {
      tool_name: "Bash",
      input: { command: "echo hi" },
      permission_suggestions: [
        { type: "addRules", rules: [{ toolName: "Bash" }], behavior: "allow", destination: "userSettings" },
      ],
    },
    always: [
      {
        id: "g0123456789abcdef",
        label: "Always allow every `Bash` call · saved to your user settings — every project",
        scope: "persistent",
        rules: ["Bash"],
      },
    ],
  });
  renderPane();
  const card = await screen.findByTestId("structured-request");
  expect(card).toHaveTextContent("as proposed — read each option's scope");
  expect(card).not.toHaveTextContent(/only the bounded/i);
});

test("a turn sent through a public link names the visitor, not 'you' (#1404)", async () => {
  const mine = turn({ text: "operator turn" });
  const theirs = turn({ text: "visitor turn", author: { name: "Alex", via: "public_link" } });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ turns: [mine, theirs] }));
  renderPane();
  const rows = await screen.findAllByTestId("structured-turn");
  expect(rows).toHaveLength(2);
  expect(within(rows[0]).getByText("you")).toBeInTheDocument();
  const label = within(rows[1]).getByText("Alex · via public link");
  expect(label).toHaveAttribute("title", expect.stringContaining("a name the visitor chose"));
  expect(within(rows[1]).queryByText("you")).toBeNull();
});

test("when attribution can't be read, no turn is labelled 'you' (#1404)", async () => {
  const t = turn({ text: "who sent this", author: { name: null, via: "unknown" } });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ turns: [t] }));
  renderPane();
  const row = await screen.findByTestId("structured-turn");
  expect(within(row).getByText("sender unknown")).toBeInTheDocument();
  expect(within(row).queryByText("you")).toBeNull();
});

// #1502: a pane drawn from a CACHED runtime hint the roster has not confirmed may be the wrong pane
// (the engine became a terminal engine). Its failed first read is not a verdict until the roster
// confirms the runtime: "Opening…", never "This conversation does not exist".
test("an unconfirmed runtime makes a 404 provisional; a confirmed one shows the verdict (#1502)", async () => {
  vi.mocked(api.structuredSnapshot).mockRejectedValue(new ApiError(404, "no such structured session"));
  const view = render(
    <MemoryRouter>
      <StructuredPane engine="codex-api" id={ID} runtimeConfirmed={false} />
    </MemoryRouter>,
  );
  expect(await screen.findByText("Opening this session…")).toBeInTheDocument();
  expect(screen.queryByText("This conversation does not exist.")).toBeNull();
  view.rerender(
    <MemoryRouter>
      <StructuredPane engine="codex-api" id={ID} runtimeConfirmed />
    </MemoryRouter>,
  );
  expect(await screen.findByText("This conversation does not exist.")).toBeInTheDocument();
});

test("an unconfirmed runtime does not hide a conversation that loads (#1502)", async () => {
  render(
    <MemoryRouter>
      <StructuredPane engine="codex-api" id={ID} runtimeConfirmed={false} />
    </MemoryRouter>,
  );
  expect(await screen.findByTestId("structured-worker")).toBeInTheDocument();
  expect(screen.queryByText("Opening this session…")).toBeNull();
});
// ---- the composer's outbox (#1465) -------------------------------------------------------------

/** A tiny server: `current` is the snapshot; `record` adds an operator turn the way submit does. */
function outboxServer(start: Partial<StructuredSnapshot> = {}) {
  const server = {
    current: snap(start),
    record(id: string, text: string, state = "queued") {
      server.current = { ...server.current, revision: server.current.revision + 1,
        turns: [...server.current.turns, turn({ turn_id: id, operation_id: id, text, reply: "", state })] };
    },
  };
  vi.mocked(api.structuredSnapshot).mockImplementation(async () => server.current);
  vi.mocked(api.structuredEvents).mockImplementation(async () =>
    ({ session_key: KEY, revision: server.current.revision, next_cursor: server.current.revision, events: [] }));
  return server;
}

/** Submit calls that wait until the test releases them. */
function heldSubmits() {
  const held: { id: string; text: string; release: (fail?: unknown) => void }[] = [];
  vi.mocked(api.structuredSubmit).mockImplementation((_k, id, text) =>
    new Promise((resolve, reject) => {
      held.push({ id, text, release: (fail) => (fail ? reject(fail) : resolve({})) });
    }));
  return held;
}

async function sendText(text: string) {
  const box = screen.getByRole("textbox");
  await userEvent.type(box, text);
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
}

test("Send empties the composer at once; the message shows as Sending… and a second one waits its turn", async () => {
  const server = outboxServer();
  const held = heldSubmits();
  renderPane();
  await screen.findByRole("textbox");
  await sendText("first");
  expect(screen.getByRole("textbox")).toHaveValue("");
  const [first] = await screen.findAllByTestId("structured-pending");
  expect(first).toHaveTextContent("Sending to BattleLab…");
  expect(screen.getByRole("button", { name: "Send" })).toBeDisabled(); // only for want of words
  await sendText("second");
  const entries = screen.getAllByTestId("structured-pending");
  expect(entries[1]).toHaveAttribute("data-phase", "waiting");
  expect(entries[1]).toHaveTextContent("Goes out after the message above.");
  expect(held).toHaveLength(1);
  server.record(held[0].id, "first");
  await act(async () => held[0].release());
  await waitFor(() => expect(held).toHaveLength(2));
  expect(held.map((h) => h.text)).toEqual(["first", "second"]);
  expect(held[1].id).not.toBe(held[0].id);
  // The recorded turn replaced its entry: one turn, no duplicate.
  expect(screen.getAllByTestId("structured-turn")).toHaveLength(1);
  expect(screen.getAllByTestId("structured-pending")).toHaveLength(1);
});

test("a snapshot that shows the turn first wins: the late POST answer is ignored", async () => {
  const server = outboxServer({ state: "running", active_turn: "busy", turns: [turn({ turn_id: "busy", state: "running" })] });
  const held = heldSubmits();
  renderPane();
  await screen.findByRole("textbox");
  await sendText("first");
  await waitFor(() => expect(held).toHaveLength(1));
  server.record(held[0].id, "first");
  await waitFor(() => expect(screen.queryByTestId("structured-pending")).toBeNull(), { timeout: 4_000 });
  await act(async () => held[0].release(new ApiError(409, "in_flight")));
  await act(async () => {});
  expect(screen.queryByTestId("structured-pending")).toBeNull(); // never resurrected as refused
  expect(screen.getAllByTestId("structured-turn")).toHaveLength(2);
});

test("a failed snapshot read neither resurrects nor drops an entry", async () => {
  outboxServer();
  vi.mocked(api.structuredSubmit).mockRejectedValue(new TypeError("network"));
  renderPane();
  await screen.findByRole("textbox");
  vi.mocked(api.structuredSnapshot).mockRejectedValue(new TypeError("offline"));
  await sendText("first");
  const pending = await screen.findByTestId("structured-pending");
  await waitFor(() => expect(pending).toHaveAttribute("data-phase", "unknown"));
  expect(screen.getAllByTestId("structured-pending")).toHaveLength(1);
});

test("Edit on a refused message releases the queue: the waiting one goes, the edit is a new id after it", async () => {
  const server = outboxServer();
  const held = heldSubmits();
  renderPane();
  await screen.findByRole("textbox");
  await sendText("first");
  await sendText("second");
  await waitFor(() => expect(held).toHaveLength(1));
  await act(async () => held[0].release(new ApiError(422, "the session is read only")));
  const [refused, waiting] = screen.getAllByTestId("structured-pending");
  await waitFor(() => expect(refused).toHaveAttribute("data-phase", "refused"));
  expect(refused).toHaveTextContent("Not sent: the session is read only");
  expect(waiting).toHaveAttribute("data-phase", "waiting");
  await act(async () => {});
  expect(held).toHaveLength(1); // the order holds behind a failure
  await userEvent.click(within(refused).getByRole("button", { name: "Edit" }));
  expect(screen.getByRole("textbox")).toHaveValue("first");
  await waitFor(() => expect(held).toHaveLength(2));
  expect(held[1].text).toBe("second");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  server.record(held[1].id, "second");
  await act(async () => held[1].release());
  await waitFor(() => expect(held).toHaveLength(3));
  expect(held[2].text).toBe("first");
  expect(held[2].id).not.toBe(held[0].id);
});

test("Edit never replaces a message being written", async () => {
  outboxServer();
  vi.mocked(api.structuredSubmit).mockRejectedValue(new ApiError(422, "no"));
  renderPane();
  await screen.findByRole("textbox");
  await sendText("first");
  const refused = await screen.findByTestId("structured-pending");
  await waitFor(() => expect(refused).toHaveAttribute("data-phase", "refused"));
  await userEvent.type(screen.getByRole("textbox"), "draft");
  expect(within(refused).getByRole("button", { name: "Edit" })).toBeDisabled();
});

test("leaving the page asks first only while something is unrecorded", async () => {
  const server = outboxServer();
  const held = heldSubmits();
  renderPane();
  await screen.findByRole("textbox");
  const prompts = () => {
    const ev = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(ev);
    return ev.defaultPrevented;
  };
  expect(prompts()).toBe(false);
  await sendText("first");
  expect(prompts()).toBe(true);
  expect(screen.getByTestId("structured-outbox-note")).toHaveTextContent(
    "Leaving drops this list; a message already on its way may still arrive");
  server.record(held[0].id, "first");
  await act(async () => held[0].release());
  await waitFor(() => expect(screen.queryByTestId("structured-pending")).toBeNull());
  expect(prompts()).toBe(false);
});

test("a pane left with a POST in flight sends nothing more when it answers late", async () => {
  const server = outboxServer();
  const held = heldSubmits();
  const view = renderPane();
  await screen.findByRole("textbox");
  await sendText("first");
  await sendText("second");
  await waitFor(() => expect(held).toHaveLength(1));
  expect(screen.getAllByTestId("structured-pending")[1]).toHaveTextContent("Goes out after the message above.");
  expect(screen.getByTestId("structured-outbox-note")).toHaveTextContent("Leaving drops this list");
  view.unmount(); // a session switch: the pane is keyed per session
  server.record(held[0].id, "first");
  await act(async () => held[0].release());
  await act(async () => {});
  expect(held).toHaveLength(1); // the waiting message went with the pane
});

test("an accepted head whose turn aged out of the snapshot is settled by the 2xx receipt's revision (Hermes on #1514)", async () => {
  const server = outboxServer();
  // The 2xx answer carries the journal revision read after recording the turn: the receipt.
  vi.mocked(api.structuredSubmit).mockResolvedValue({ state: "queued", revision: 9 });
  renderPane();
  await screen.findByRole("textbox");
  vi.mocked(api.structuredSnapshot).mockRejectedValue(new TypeError("offline")); // reads fail after the 2xx
  await sendText("first");
  await sendText("second");
  await waitFor(() => expect(api.structuredSubmit).toHaveBeenCalledTimes(1));
  const [head] = screen.getAllByTestId("structured-pending");
  await waitFor(() => expect(head).toHaveTextContent("Recorded by BattleLab"));
  // Reads recover; the conversation moved on, and the head's completed turn is no longer listed.
  server.current = { ...server.current, revision: 70, turns: [turn({ text: "much later" })] };
  vi.mocked(api.structuredSnapshot).mockImplementation(async () => server.current);
  await waitFor(() => expect(api.structuredSubmit).toHaveBeenCalledTimes(2), { timeout: 4_000 });
  expect(vi.mocked(api.structuredSubmit).mock.calls[1][2]).toBe("second");
  await waitFor(() => expect(screen.queryByText("first")).toBeNull()); // no pending row left for it
});

test("a recovered read BEFORE the receipt's revision does not settle the head", async () => {
  const server = outboxServer();
  vi.mocked(api.structuredSubmit).mockResolvedValue({ state: "queued", revision: 9 });
  renderPane();
  await screen.findByRole("textbox");
  vi.mocked(api.structuredSnapshot).mockRejectedValue(new TypeError("offline"));
  await sendText("first");
  await sendText("second");
  await waitFor(() => expect(screen.getAllByTestId("structured-pending")[0]).toHaveTextContent("Recorded by BattleLab"));
  server.current = { ...server.current, revision: 8 }; // older than the receipt, without the turn
  vi.mocked(api.structuredSnapshot).mockImplementation(async () => server.current);
  await new Promise((r) => setTimeout(r, 2_500)); // two active poll cycles
  expect(api.structuredSubmit).toHaveBeenCalledTimes(1);
  expect(screen.getAllByTestId("structured-pending")).toHaveLength(2);
});
