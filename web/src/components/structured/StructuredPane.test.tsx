import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo, StructuredSnapshot, StructuredTurn } from "../../types/api";
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
    },
  };
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

function renderPane() {
  return render(
    <MemoryRouter>
      <StructuredPane engine="codex-api" id={ID} />
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
  expect(buttons).toEqual(["Approve once", "Reject"]);
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

test("a stale-revision refusal keeps the draft and says why", async () => {
  vi.mocked(api.structuredSubmit).mockRejectedValue(new ApiError(409, "the conversation changed (revision 4 → 5)"));
  renderPane();
  const box = await screen.findByRole("textbox");
  await userEvent.type(box, "run the logout spec too");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByTestId("structured-error")).toHaveTextContent("revision 4 → 5");
  expect(box).toHaveValue("run the logout spec too");
});

test("a send with an unknown outcome reuses its operation id on retry", async () => {
  vi.mocked(api.structuredSubmit).mockRejectedValueOnce(new TypeError("network")).mockResolvedValueOnce({});
  renderPane();
  const box = await screen.findByRole("textbox");
  await userEvent.type(box, "hello");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByTestId("structured-error");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  const [a, b] = vi.mocked(api.structuredSubmit).mock.calls;
  expect(b[1]).toBe(a[1]);
  expect(b[2]).toBe("hello");
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
  expect(screen.getByTestId("structured-worker")).toHaveTextContent("reconnecting");
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
  expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();
});

test("stop reports the proved containment, not just the click", async () => {
  vi.mocked(api.structuredStop).mockResolvedValue({ containment: "gone" });
  renderPane();
  await userEvent.click(await screen.findByRole("button", { name: "Stop" }));
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
  await userEvent.click(screen.getByRole("button", { name: "Stop" }));
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
  await userEvent.click(screen.getByRole("button", { name: "Stop" })); // triggers a re-read → rev 4
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

// ---- the pane head (#1332) ---------------------------------------------------------------------

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
    expect(items).toEqual(["Files", "Recap", "Hand off", "Share link"]);
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
    expect(screen.queryByTestId("structured-worker")).toBeNull(); // no second bar
    const chips = within(slot);
    expect(chips.getByRole("button", { name: "Browse session files" })).toBeInTheDocument();
    await userEvent.click(chips.getByRole("button", { name: "Stop the worker" }));
    expect(api.structuredStop).toHaveBeenCalledWith(KEY);
  } finally {
    slot.remove();
  }
});
