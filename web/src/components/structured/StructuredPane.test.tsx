import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import { imageFilesFromAsyncClipboard } from "../../lib/clipboardImages";
import { appendSent, clearSent, readSent } from "../../lib/sentHistory";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo, StructuredSnapshot, StructuredTurn, Template } from "../../types/api";
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
      upload: vi.fn(),
      templates: vi.fn(),
      templateVariables: vi.fn(() => Promise.resolve({ variables: [], limits: {} })),
      uploadBlob: vi.fn(() => new Promise(() => {})),
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
    expect(items).toEqual(["Files", "Recap", "Hand off", "Share link", "Stop"]);
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

test("a client that takes no pictures offers no attach", async () => {
  renderPane();
  await screen.findByRole("textbox");
  expect(screen.queryByRole("button", { name: "Attach images" })).toBeNull();
});

test("attached pictures ride the send by upload name; changing them mints a new operation", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  vi.mocked(api.structuredSubmit).mockRejectedValueOnce(new TypeError("network")).mockResolvedValue({});
  uploaded(1);
  uploaded(2);
  renderPane();
  await screen.findByRole("button", { name: "Attach images" });
  await userEvent.upload(screen.getByTestId("structured-file-input"), [png("a.png"), png("b.png")]);
  expect(await screen.findAllByTestId("structured-attachment")).toHaveLength(2);
  await userEvent.type(screen.getByRole("textbox"), "what is wrong here");
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByTestId("structured-error"); // unknown outcome: kept for a safe retry
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

test("a paste while a message is sending joins nothing: no upload, no stray chip after", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap({ images: true }));
  let post!: () => void;
  vi.mocked(api.structuredSubmit).mockImplementationOnce(
    () => new Promise((r) => (post = () => r({}))),
  );
  uploaded(6);
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
  await act(async () => post());
  await waitFor(() => expect(screen.queryAllByTestId("structured-attachment")).toHaveLength(0));
  expect(api.upload).toHaveBeenCalledTimes(1); // only the picture that was sent
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
  await screen.findByTestId("structured-error");
  let [entry] = readSent();
  expect(readSent()).toHaveLength(1);
  expect(entry).toMatchObject({
    text: "look at this  ", // untrimmed, so Restore round-trips it
    attachments: ["20261008-010007-shot.png"],
    session: KEY,
    confirmed: false,
  });
  await userEvent.click(screen.getByRole("button", { name: "Send" })); // the safe retry
  await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue(""));
  expect(readSent()).toHaveLength(1); // the same operation: no second entry
  [entry] = readSent();
  expect(entry.confirmed).toBe(true);
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
  await screen.findByTestId("structured-error");
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

test("a held retry the conversation shows as recorded is retired: Send can never replay it (Hermes on #1346)", async () => {
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
  await screen.findByTestId("structured-error");
  expect(box).toHaveValue("deploy it"); // kept for a safe retry
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15_000); // the periodic re-read finds the turn
  });
  await waitFor(() => expect(box).toHaveValue("")); // it WAS sent: the draft is cleared
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
