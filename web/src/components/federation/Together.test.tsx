import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useReducer, type ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../../app/config";
import { ApiError } from "../../lib/api";
import { REMOTE_ROUTE, UNKNOWN_DATE, UNKNOWN_TIME, remotePath } from "../../lib/federation";
import { notesInit, notesReduce, threadOf } from "../../lib/federationTogether";
import type { AppConfig } from "../../types/api";

// Working together (#2062) in jsdom, for what the browser spec cannot plant or cannot see:
// StrictMode's effect replay (main.tsx mounts the app under <StrictMode>) for every loader this
// UI adds, a peer's numbers that JSON cannot carry (NaN, Infinity) or that no `Date` can hold,
// a terminal row reaching the conversation pane, and the exact number of writes a refusal makes.
// The journeys are e2e/peer-together.spec.ts's.
const api = vi.hoisted(() => ({
  federationRemote: vi.fn(),
  federationRemoteSnapshot: vi.fn(),
  federationRemoteEvents: vi.fn(),
  federationRemoteHistory: vi.fn(),
  federationRemoteFrame: vi.fn(),
  federationRemoteSuggestions: vi.fn(),
  federationRemoteOperation: vi.fn(),
  federationRemoteAccess: vi.fn(),
  askFederationRemoteAccess: vi.fn(),
  withdrawFederationRemoteAccess: vi.fn(),
  federationRemoteDecide: vi.fn(),
  federationRemoteNotes: vi.fn(),
  readFederationRemoteNotes: vi.fn(),
  addFederationRemoteNote: vi.fn(),
  federationNotes: vi.fn(),
  readFederationNotes: vi.fn(),
  federationGrants: vi.fn(),
  prepareFederationContinue: vi.fn(),
  createFederationContinue: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  api,
}));

const { RemotePane } = await import("./RemotePane");
const { RemoteSessionView } = await import("./RemoteSessionView");
const { AskAccessDialog } = await import("./AskAccessDialog");
const { PeerDecisionCard } = await import("./PeerDecisions");
const { ContinueHereDialog } = await import("./ContinueHereDialog");
const { NotesDrawer } = await import("./NotesDrawer");
const { ShareStrip } = await import("./ShareStrip");
const { refreshRemoteShares, resetRemoteSharesForTest } = await import("../../app/remoteShares");

const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const GRANT = "g7Jx9aBcDeFgHiJkLmNoPq";
const GRANT_B = "h8Ky0bCdEfGhIjKlMnOpQr";
const KEY = "codex-api:0d0f6a52-1111-4222-8333-444455556666";
const nowS = () => Date.now() / 1000;
const HOSTILE = [1e300, Number.NaN, Number.POSITIVE_INFINITY, -1, "1800000000"] as const;

const row = (over: Record<string, unknown> = {}) => ({
  key: `peer:${GPU}/codex-api:0d0f6a52-1111-4222-8333-444455556666`,
  peer_id: GPU,
  peer_name: "gpu-box",
  connected: true,
  grant_id: GRANT,
  engine: "codex-api",
  native_id: "0d0f6a52-1111-4222-8333-444455556666",
  title: "Tune the render queue",
  kind: "api",
  level: "view",
  expires_at: nowS() + 7 * 86400,
  revision: 3,
  seen_at: nowS() - 30,
  ...over,
});

const OFFER = "Zm9vYmFy-_Zm9vYmFy-_Zm9vYmFy-_01";
const FRESH_OFFER = "cXV1eA_-cXV1eA_-cXV1eA_-cXV1eA02";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const REQUEST = {
  request_id: "req-1",
  turn_id: "aaaaaaaa-0000-4000-8000-000000000001",
  kind: "command",
  summary: "npm test",
  choices: ["approve", "reject"],
  offer: OFFER,
};

const snapshot = (over: Record<string, unknown> = {}) => ({
  link: "L".repeat(24),
  revision: 5,
  event_cursor: 5,
  turns: [
    {
      turn_id: "aaaaaaaa-0000-4000-8000-000000000001",
      kind: "operator",
      state: "completed",
      text: "Find out why the queue stalls.",
      reply: "One lock for the whole batch.",
    },
  ],
  omitted_turns: 0,
  pending_requests: [],
  can_send: false,
  ...over,
});

const held = (...args: unknown[]) => {
  const signal = args[args.length - 1] as AbortSignal | undefined;
  return new Promise((_resolve, reject) => signal?.addEventListener?.("abort", () => reject(new Error("aborted"))));
};
const never = () => new Promise(() => {});

const grantRow = (over: Record<string, unknown> = {}) => ({
  grant_id: GRANT,
  session_key: KEY,
  peer_id: GPU,
  peer_name: "gpu-box",
  kind: "api",
  level: "view",
  expires_at: nowS() + 5 * 86400,
  revision: 1,
  state: "live",
  created_at: nowS() - 60,
  ended_at: null,
  end_reason: null,
  watching: false,
  notes: 0,
  ...over,
});

beforeEach(() => {
  resetRemoteSharesForTest();
  sessionStorage.clear();
  localStorage.clear();
  Object.values(api).forEach((f) => f.mockReset());
  api.federationRemote.mockResolvedValue({ remote: [row()] });
  api.federationRemoteSnapshot.mockResolvedValue(snapshot());
  api.federationRemoteEvents.mockImplementation(held);
  api.federationRemoteHistory.mockImplementation(never);
  api.federationRemoteFrame.mockImplementation(held);
  api.federationRemoteSuggestions.mockResolvedValue({ suggestions: [] });
  api.federationRemoteOperation.mockResolvedValue({ known: false });
  api.federationRemoteAccess.mockResolvedValue({ level: "view", request: null });
  api.federationRemoteNotes.mockResolvedValue({ reset: false, seq: 0, changes: [], more: false, next: null, unread: 0 });
  api.readFederationRemoteNotes.mockResolvedValue({ unread: 0 });
  api.readFederationNotes.mockResolvedValue({ unread: 0 });
  api.federationGrants.mockResolvedValue({ grants: [], generation: 1 });
});

const wrap = (strict: boolean, tree: ReactNode) => (strict ? <StrictMode>{tree}</StrictMode> : tree);

async function pane(strict = false, path = remotePath(GPU, GRANT)) {
  await refreshRemoteShares(); // the mirror lists the share before the pane opens, as in the app
  return render(
    wrap(
      strict,
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={REMOTE_ROUTE} element={<RemotePane peerId={GPU} grantId={GRANT} />} />
        </Routes>
      </MemoryRouter>,
    ),
  );
}

// ---- a terminal share: none of working together, in either pane (#2060) ---------------------

const together = ["remote-menu", "remote-presence", "remote-decisions", "remote-access", "remote-access-ask"];
const busySnapshot = () =>
  snapshot({ owner_present: true, pending_requests: [REQUEST], decisions: [{ request_id: "old", turn_id: "t", answer: "once", author: { via: "owner" } }] });

test("an API row at view: the ⋯ menu, the presence line, the decision card and Ask for access", async () => {
  api.federationRemoteSnapshot.mockResolvedValue(busySnapshot());
  await pane();
  expect(await screen.findByTestId("remote-presence")).toHaveTextContent("gpu-box's operator is here");
  expect(screen.getByTestId("remote-menu")).toBeInTheDocument();
  expect(screen.getByTestId("remote-decisions")).toBeInTheDocument();
  expect(screen.getAllByTestId("remote-access-ask").length).toBeGreaterThan(0);
  await waitFor(() => expect(api.federationRemoteAccess).toHaveBeenCalled());
});

test.each(["view", "suggest", "send"])(
  "a row that says terminal (%s) gets nothing of working together in the conversation pane, and nothing is asked",
  async (level) => {
    api.federationRemote.mockResolvedValue({ remote: [row({ kind: "terminal", level })] });
    api.federationRemoteSnapshot.mockResolvedValue(busySnapshot());
    await pane(false, `${remotePath(GPU, GRANT)}?notes=1`);
    await screen.findByText("One lock for the whole batch.");
    for (const id of together) expect(screen.queryByTestId(id), id).toBeNull();
    expect(screen.queryByTestId("notes-drawer")).toBeNull(); // not even from a bell link
    expect(screen.queryByTestId("peer-decision")).toBeNull();
    expect(api.federationRemoteAccess).not.toHaveBeenCalled();
    expect(api.federationRemoteNotes).not.toHaveBeenCalled();
  },
);

test("a terminal share opens the screen pane, which has no menu, presence, notes or access foot", async () => {
  api.federationRemote.mockResolvedValue({ remote: [row({ kind: "terminal", engine: "claude", level: "view" })] });
  render(
    <MemoryRouter initialEntries={[`${remotePath(GPU, GRANT)}?notes=1`]}>
      <Routes>
        <Route path={REMOTE_ROUTE} element={<RemoteSessionView />} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() => expect(api.federationRemoteFrame).toHaveBeenCalled());
  for (const id of [...together, "notes-drawer", "continue-here", "ask-access"]) expect(screen.queryByTestId(id), id).toBeNull();
  expect(api.federationRemoteAccess).not.toHaveBeenCalled();
  expect(api.federationRemoteSnapshot).not.toHaveBeenCalled();
});

// ---- a write is sent once, and its real outcome is shown -------------------------------------

async function waiting() {
  api.federationRemoteAccess.mockResolvedValue({
    level: "view",
    request: { request_id: "r".repeat(22), level: "send", state: "pending" },
  });
  await pane();
  return await screen.findByTestId("remote-access-withdraw");
}

test("a refused Withdraw (404) says so and reads the list again, after exactly one call", async () => {
  const button = await waiting();
  const lists = api.federationRemote.mock.calls.length;
  api.withdrawFederationRemoteAccess.mockRejectedValue(new ApiError(404, "no longer shared", { code: "no_grant" }));
  await userEvent.click(button);
  expect(await screen.findByTestId("remote-access-error")).toHaveTextContent(
    "Not withdrawn. gpu-box refused it: this share ended or changed.",
  );
  await waitFor(() => expect(api.federationRemote.mock.calls.length).toBeGreaterThan(lists));
  expect(api.withdrawFederationRemoteAccess).toHaveBeenCalledTimes(1);
});

test("a Withdraw while the peer is offline (503) is the pane's offline state, after exactly one call", async () => {
  const button = await waiting();
  api.withdrawFederationRemoteAccess.mockRejectedValue(new ApiError(503, "offline", { code: "peer_offline" }));
  await userEvent.click(button);
  expect(await screen.findByTestId("remote-empty")).toHaveTextContent("gpu-box is not connected");
  expect(api.withdrawFederationRemoteAccess).toHaveBeenCalledTimes(1);
});

test("a Withdraw the owner did not take (409) says so, re-reads the status, and is not sent again", async () => {
  const button = await waiting();
  const reads = api.federationRemoteAccess.mock.calls.length;
  api.withdrawFederationRemoteAccess.mockRejectedValue(new ApiError(409, "that request is already granted", { code: "conflict" }));
  await userEvent.click(button);
  expect(await screen.findByTestId("remote-access-error")).toHaveTextContent("Not withdrawn. that request is already granted");
  await waitFor(() => expect(api.federationRemoteAccess.mock.calls.length).toBeGreaterThan(reads));
  expect(api.withdrawFederationRemoteAccess).toHaveBeenCalledTimes(1);
});

test("a Withdraw with no answer is 'could not confirm', never a second Withdraw", async () => {
  const button = await waiting();
  api.withdrawFederationRemoteAccess.mockRejectedValue(new TypeError("Failed to fetch"));
  await userEvent.click(button);
  expect(await screen.findByTestId("remote-access-error")).toHaveTextContent("Could not confirm that the request was withdrawn.");
  expect(api.withdrawFederationRemoteAccess).toHaveBeenCalledTimes(1);
});

// ---- StrictMode: every loader and answer of working together ----------------------------------

test("under StrictMode a decision answer is taken and shown, once", async () => {
  api.federationRemote.mockResolvedValue({ remote: [row({ level: "send" })] });
  api.federationRemoteSnapshot.mockResolvedValue(snapshot({ pending_requests: [REQUEST] }));
  api.federationRemoteDecide.mockResolvedValue({ outcome: "accepted", author: { via: "peer", name: "studio" } });
  await pane(true);
  const card = await screen.findByTestId("peer-decision");
  await userEvent.click(within(card).getByRole("button", { name: "Approve once" }));
  expect(await within(card).findByTestId("peer-decision-outcome")).toHaveTextContent("Your answer was taken.");
  expect(api.federationRemoteDecide).toHaveBeenCalledTimes(1);
});

// ---- an answer names the offer its snapshot showed (the owner requires it) ---------------------

const sendPane = async (pending: unknown[]) => {
  api.federationRemote.mockResolvedValue({ remote: [row({ level: "send" })] });
  api.federationRemoteSnapshot.mockResolvedValue(snapshot({ pending_requests: pending }));
  await pane();
  return (await screen.findAllByTestId("peer-decision"))[0];
};
const decideBody = (n: number) => api.federationRemoteDecide.mock.calls[n][2] as Record<string, unknown>;

test("an answer carries the snapshot's offer of that request, and a retry keeps the offer and the key", async () => {
  const card = await sendPane([{ ...REQUEST, request_id: "req-0", offer: FRESH_OFFER, summary: "ls" }, REQUEST]);
  const mine = screen.getAllByTestId("peer-decision").find((c) => c.getAttribute("data-request") === "req-1")!;
  expect(card).toBeInTheDocument();
  api.federationRemoteDecide.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  api.federationRemoteDecide.mockResolvedValueOnce({ outcome: "accepted", author: { via: "peer", name: "studio" } });
  await userEvent.click(within(mine).getByRole("button", { name: "Approve once" }));
  expect(await within(mine).findByTestId("peer-decision-outcome")).toHaveAttribute("data-outcome", "unconfirmed");
  expect(api.federationRemoteDecide).toHaveBeenCalledTimes(1);
  // The offer of THE SAME entry whose request id is sent, as it came; nothing else is added.
  expect(decideBody(0)).toEqual({
    request_id: "req-1",
    offer: OFFER,
    choice: "approve",
    client_key: expect.stringMatching(UUID_RE),
  });
  await userEvent.click(within(mine).getByRole("button", { name: "Retry: Approve once" }));
  await waitFor(() => expect(within(mine).getByTestId("peer-decision-outcome")).toHaveAttribute("data-outcome", "accepted"));
  expect(api.federationRemoteDecide).toHaveBeenCalledTimes(2);
  expect(decideBody(1)).toEqual(decideBody(0)); // the same offer AND the same key
});

// ---- an outcome that is not confirmed (#2061's `504 peer_lost`, and anything unknown) ----------

const LOST = () => new ApiError(504, "the connection ended before it answered", { code: "peer_lost" });
/** Failures nobody listed: each may have happened after the effect. */
const UNKNOWN = {
  "504 peer_lost": LOST,
  "429 busy": () => new ApiError(429, "busy", { code: "busy" }),
  "an unknown status": () => new ApiError(418, "teapot", { code: "teapot" }),
  "a known status with an unknown code": () => new ApiError(409, "odd", { code: "something_new" }),
  "no answer": () => new TypeError("Failed to fetch"),
} as const;

test.each(Object.keys(UNKNOWN) as (keyof typeof UNKNOWN)[])(
  "a decision answered with %s is not confirmed: only the same answer can go again, under the same key and offer",
  async (name) => {
    const card = await sendPane([REQUEST]);
    const reads = api.federationRemoteSnapshot.mock.calls.length;
    api.federationRemoteDecide.mockRejectedValueOnce(UNKNOWN[name]());
    api.federationRemoteDecide.mockResolvedValueOnce({ outcome: "accepted", author: { via: "peer", name: "studio" } });
    await userEvent.click(within(card).getByRole("button", { name: "Approve once" }));
    const outcome = await within(card).findByTestId("peer-decision-outcome");
    expect(outcome).toHaveAttribute("data-outcome", "unconfirmed");
    expect(outcome).toHaveTextContent("Could not confirm that answer. It may have been taken.");
    expect(card).not.toHaveTextContent(/not taken|Nothing was answered|not accepted/i);
    // No fresh attempt: the other answer is off, and the one that was pressed is a retry.
    expect(within(card).getByRole("button", { name: "Reject" })).toBeDisabled();
    const retry = within(card).getByRole("button", { name: "Retry: Approve once" });
    expect(retry).toBeEnabled();
    // The session is read again: its `decisions` are what settles the request.
    await waitFor(() => expect(api.federationRemoteSnapshot.mock.calls.length).toBeGreaterThan(reads));
    expect(api.federationRemoteDecide).toHaveBeenCalledTimes(1);
    await userEvent.click(retry);
    await waitFor(() => expect(within(card).getByTestId("peer-decision-outcome")).toHaveAttribute("data-outcome", "accepted"));
    expect(decideBody(1)).toEqual(decideBody(0)); // the same press: the same key, the same offer
  },
);

test("a question that was not confirmed can only be sent again with the options it was answered with", async () => {
  const QUESTION = {
    ...REQUEST,
    kind: "question",
    choices: ["answer", "reject"],
    questions: [{ question: "Which queue?", multi: false, options: ["fast", "slow"] }],
  };
  const card = await sendPane([QUESTION]);
  api.federationRemoteDecide.mockRejectedValueOnce(LOST());
  api.federationRemoteDecide.mockResolvedValueOnce({ outcome: "accepted", author: { via: "peer", name: "studio" } });
  await userEvent.click(within(card).getByRole("radio", { name: "slow" }));
  await userEvent.click(within(card).getByRole("button", { name: "Send answer" }));
  await within(card).findByTestId("peer-decision-outcome");
  for (const option of within(card).getAllByTestId("peer-decision-option")) expect(option).toBeDisabled();
  expect(within(card).getByRole("radio", { name: "slow" })).toHaveAttribute("aria-checked", "true");
  expect(within(card).getByRole("button", { name: "Skip question" })).toBeDisabled();
  await userEvent.click(within(card).getByRole("button", { name: "Retry: Send answer" }));
  await waitFor(() => expect(api.federationRemoteDecide).toHaveBeenCalledTimes(2));
  expect(decideBody(1)).toEqual(decideBody(0));
  expect(decideBody(0)).toMatchObject({ choice: "answer", options: [[1]] });
});

// ---- a question's selection belongs to the offer it was made for ---------------------------------

const KEEP_OR_DELETE = {
  ...REQUEST,
  kind: "question",
  choices: ["answer", "reject"],
  questions: [{ question: "What about the old branch?", multi: false, options: ["Keep", "Delete"] }],
};
/** The owner replaced the request under the SAME turn and request id: index 0 is now "Delete". */
const REPLACED = {
  ...KEEP_OR_DELETE,
  offer: FRESH_OFFER,
  questions: [{ question: "What about the old branch?", multi: false, options: ["Delete", "Keep"] }],
};
const { cleanRequest } = await import("../../lib/federationTogether");

test.each([false, true])(
  "a card re-rendered with the same ids and a new offer has nothing selected and cannot send (multi: %s)",
  async (multi) => {
    const of = (raw: typeof KEEP_OR_DELETE) =>
      cleanRequest({ ...raw, questions: raw.questions.map((q) => ({ ...q, multi })) })!;
    const onAnswer = vi.fn();
    // No `key`: the component itself must drop what was picked for the other offer.
    const { rerender } = render(
      <PeerDecisionCard request={of(KEEP_OR_DELETE)} level="send" owner="gpu-box" state={null} onAnswer={onAnswer} />,
    );
    const role = multi ? "checkbox" : "radio";
    await userEvent.click(screen.getByRole(role, { name: "Keep" }));
    expect(screen.getByRole(role, { name: "Keep" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("button", { name: "Send answer" })).toBeEnabled();
    rerender(<PeerDecisionCard request={of(REPLACED)} level="send" owner="gpu-box" state={null} onAnswer={onAnswer} />);
    for (const option of screen.getAllByTestId("peer-decision-option")) expect(option).toHaveAttribute("aria-checked", "false");
    const send = screen.getByRole("button", { name: "Send answer" });
    expect(send).toBeDisabled();
    await userEvent.click(send);
    expect(onAnswer).not.toHaveBeenCalled();
    // A fresh choice is what is sent: the index picked NOW, under the card's current request.
    await userEvent.click(screen.getByRole(role, { name: "Keep" }));
    await userEvent.click(screen.getByRole("button", { name: "Send answer" }));
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer).toHaveBeenCalledWith("answer", [[1]]);
    // …and going back to the first offer does not bring its old pick back either.
    rerender(<PeerDecisionCard request={of(KEEP_OR_DELETE)} level="send" owner="gpu-box" state={null} onAnswer={onAnswer} />);
    for (const option of screen.getAllByTestId("peer-decision-option")) expect(option).toHaveAttribute("aria-checked", "false");
  },
);

test("a retry lock from an attempt at another offer does not lock the replacement's card", async () => {
  const onAnswer = vi.fn();
  const stale = {
    attempt: { requestKey: "k", answer: JSON.stringify({ choice: "answer", options: [[0]] }), offer: OFFER, client_key: "c" },
    phase: "done" as const,
    outcome: { kind: "unconfirmed" as const, text: "x" },
  };
  render(<PeerDecisionCard request={cleanRequest(REPLACED)!} level="send" owner="gpu-box" state={stale} onAnswer={onAnswer} />);
  expect(screen.queryByRole("button", { name: /^Retry:/ })).toBeNull();
  for (const option of screen.getAllByTestId("peer-decision-option")) {
    expect(option).toBeEnabled();
    expect(option).toHaveAttribute("aria-checked", "false"); // not the old attempt's index 0
  }
  expect(screen.getByRole("button", { name: "Send answer" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Skip question" })).toBeEnabled();
});

test("a refreshed snapshot that swaps the question under the same ids clears the selection in the pane", async () => {
  // The journal moves once: the pane's own loop reads the snapshot again.
  let move!: () => void;
  const moved = new Promise<void>((r) => (move = r));
  api.federationRemoteEvents.mockImplementationOnce(() =>
    moved.then(() => ({ link: "L".repeat(24), revision: 5, next_cursor: 6, events: [{ kind: "request" }] })),
  );
  const card = await sendPane([KEEP_OR_DELETE]);
  await userEvent.click(within(card).getByRole("radio", { name: "Keep" })); // index 0
  expect(within(card).getByRole("button", { name: "Send answer" })).toBeEnabled();
  api.federationRemoteSnapshot.mockResolvedValue(snapshot({ event_cursor: 6, pending_requests: [REPLACED] }));
  await act(async () => move());
  // The replacement is on screen: index 0 now reads "Delete", and it is NOT selected.
  await waitFor(() =>
    expect(screen.getAllByTestId("peer-decision-option").map((o) => o.textContent)).toEqual(["Delete", "Keep"]),
  );
  const now = screen.getByTestId("peer-decision");
  for (const option of within(now).getAllByTestId("peer-decision-option")) expect(option).toHaveAttribute("aria-checked", "false");
  const send = within(now).getByRole("button", { name: "Send answer" });
  expect(send).toBeDisabled();
  await userEvent.click(send);
  expect(api.federationRemoteDecide).not.toHaveBeenCalled();
  // A fresh pick goes out with the NEW offer and the index picked now.
  api.federationRemoteDecide.mockResolvedValue({ outcome: "accepted", author: { via: "peer", name: "studio" } });
  await userEvent.click(within(now).getByRole("radio", { name: "Keep" }));
  await userEvent.click(within(now).getByRole("button", { name: "Send answer" }));
  await waitFor(() => expect(api.federationRemoteDecide).toHaveBeenCalledTimes(1));
  expect(decideBody(0)).toMatchObject({ request_id: "req-1", offer: FRESH_OFFER, choice: "answer", options: [[1]] });
});

test("a decision the snapshot settles after a lost answer shows who answered, and nothing is sent again", async () => {
  const card = await sendPane([REQUEST]);
  api.federationRemoteDecide.mockRejectedValueOnce(LOST());
  // The owner DID take it: the re-read no longer lists the request, and names this instance.
  api.federationRemoteSnapshot.mockResolvedValue(
    snapshot({
      decisions: [{ request_id: "req-1", turn_id: REQUEST.turn_id, answer: "once", author: { via: "peer", name: "studio" }, mine: true }],
    }),
  );
  await userEvent.click(within(card).getByRole("button", { name: "Approve once" }));
  const settled = await screen.findByTestId("settled-decision");
  expect(within(settled).getByTestId("settled-by")).toHaveTextContent("answered by you");
  expect(screen.queryByTestId("peer-decision")).toBeNull();
  expect(api.federationRemoteDecide).toHaveBeenCalledTimes(1);
});

test.each(Object.keys(UNKNOWN) as (keyof typeof UNKNOWN)[])(
  "a note added with %s keeps its row and its key: Retry is the same note, and nothing says it was not saved",
  async (name) => {
    render(<Thread side="peer" grantId={GRANT} />);
    await waitFor(() => expect(api.federationRemoteNotes).toHaveBeenCalled());
    const reads = api.federationRemoteNotes.mock.calls.length;
    api.addFederationRemoteNote.mockRejectedValueOnce(UNKNOWN[name]());
    fireEvent.change(screen.getByTestId("notes-box"), { target: { value: "the lock is per job" } });
    await userEvent.click(screen.getByTestId("notes-send"));
    const pending = await screen.findByTestId("note-pending");
    await waitFor(() => expect(pending).toHaveAttribute("data-state", "unconfirmed"));
    expect(pending).toHaveTextContent("not confirmed");
    expect(pending).toHaveTextContent("It may have been saved. Retry sends the same note, not a second one.");
    expect(screen.queryByTestId("notes-refusal")).toBeNull(); // never "nothing was saved"
    expect(screen.getByTestId("notes-box")).toHaveValue("the lock is per job");
    // The thread is read again at once: a note that arrived by that read settles the row.
    await waitFor(() => expect(api.federationRemoteNotes.mock.calls.length).toBeGreaterThan(reads));
    expect(api.addFederationRemoteNote).toHaveBeenCalledTimes(1);
    const saved = note("mine", { author: "peer", mine: true, text: "the lock is per job", created_at: nowS() });
    api.addFederationRemoteNote.mockResolvedValueOnce({ note: saved, seq: 1 });
    await userEvent.click(within(pending).getByTestId("note-retry"));
    await waitFor(() => expect(screen.queryByTestId("note-pending")).toBeNull());
    const [first, second] = api.addFederationRemoteNote.mock.calls.map((c) => c[2] as Record<string, unknown>);
    expect(second).toEqual(first); // the same client_key and text: the owner dedupes by key
    expect(first.client_key).toMatch(UUID_RE);
    expect(screen.getAllByTestId("note")).toHaveLength(1);
  },
);

test("a note whose answer was lost is settled by the next read, without a second add", async () => {
  render(<Thread side="peer" grantId={GRANT} />);
  await waitFor(() => expect(api.federationRemoteNotes).toHaveBeenCalled());
  const saved = note("mine", { author: "peer", mine: true, text: "kept", created_at: nowS() });
  api.addFederationRemoteNote.mockRejectedValueOnce(LOST());
  api.federationRemoteNotes.mockResolvedValue({
    reset: false,
    seq: 1,
    changes: [{ seq: 1, note_id: saved.note_id, note: saved }],
    more: false,
    next: null,
    unread: 0,
  });
  fireEvent.change(screen.getByTestId("notes-box"), { target: { value: "kept" } });
  await userEvent.click(screen.getByTestId("notes-send"));
  expect(await screen.findByTestId("note")).toHaveTextContent("kept");
  await waitFor(() => expect(screen.queryByTestId("note-pending")).toBeNull());
  expect(screen.getByTestId("notes-box")).toHaveValue("");
  expect(api.addFederationRemoteNote).toHaveBeenCalledTimes(1);
});

// ---- one read per thread at a time; an older answer never lands over a newer one ---------------

/** Every notes read is held until the test answers it, in the order the test chooses. */
function heldReads() {
  const calls: { after: unknown; answer: (page: unknown) => void }[] = [];
  api.federationRemoteNotes.mockImplementation(
    (_peer: string, _grant: string, after: unknown) =>
      new Promise((resolve) => calls.push({ after, answer: resolve })),
  );
  return calls;
}
const changes = (seq: number, list: unknown[]) => ({ reset: false, seq, changes: list, more: false, next: null, unread: 0 });
const hiddenNote = () => note("h", { text: "HIDDEN-NOTE" });
const tick = () => act(async () => void vi.advanceTimersByTime(4_000));

test("a poll tick during a read in flight starts no second request, and exactly one read follows", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  try {
    const HIDDEN = hiddenNote();
    const calls = heldReads();
    render(<Thread side="peer" grantId={GRANT} />);
    await waitFor(() => expect(calls).toHaveLength(1));
    await tick();
    await tick();
    await tick();
    expect(calls).toHaveLength(1); // three ticks, still the one read
    await act(async () => calls[0].answer(changes(1, [{ seq: 1, note_id: HIDDEN.note_id, note: HIDDEN }])));
    await waitFor(() => expect(calls).toHaveLength(2)); // ONE follow-up for all three
    expect(calls[1].after).toBe(1); // asked from where the thread now stands
    await act(async () => calls[1].answer(changes(1, [])));
    await act(async () => {});
    expect(calls).toHaveLength(2); // and no third until the next tick
    await tick();
    expect(calls).toHaveLength(3);
  } finally {
    vi.useRealTimers();
  }
});

test("a delayed older notes answer cannot put back a note the newer answer removed", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  try {
    const HIDDEN = hiddenNote();
    const calls = heldReads();
    render(<Thread side="peer" grantId={GRANT} />);
    await waitFor(() => expect(calls).toHaveLength(1)); // read A, asked from 0, delayed
    await tick(); // the next poll comes while A is still in flight
    const A = changes(1, [{ seq: 1, note_id: HIDDEN.note_id, note: HIDDEN }]);
    const B = changes(2, [{ seq: 2, note_id: HIDDEN.note_id, removed: true }]);
    // If a second read was started (it must not be), it is the NEWER one and lands first.
    const done = new Set<number>([0]);
    if (calls.length > 1) {
      done.add(1);
      await act(async () => calls[1].answer(B));
    }
    await act(async () => calls[0].answer(A));
    // Whatever was in flight has landed. Every read that follows is answered as the owner
    // would answer it from the cursor it asks after.
    for (let round = 0; round < 5; round++) {
      await act(async () => {
        calls.forEach((call, i) => {
          if (done.has(i)) return;
          done.add(i);
          call.answer(call.after === 2 ? changes(2, []) : call.after === 1 ? B : A);
        });
      });
      await tick();
    }
    expect(calls.length).toBeGreaterThan(2);
    // The owner hid the note: it is not on screen, however the answers were ordered.
    expect(screen.queryByText("HIDDEN-NOTE")).toBeNull();
    expect(screen.queryAllByTestId("note")).toHaveLength(0);
    expect(calls[calls.length - 1].after).toBe(2); // the cursor is past the removal
  } finally {
    vi.useRealTimers();
  }
});

// ---- a write's late answer never moves a thread back -------------------------------------------

/** A write whose answer the test gives when it chooses. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}
/** Answer the next notes read that has not been answered, after a poll tick if none is waiting. */
function reader(calls: ReturnType<typeof heldReads>) {
  let done = 0;
  return async (answer: (after: unknown) => unknown) => {
    if (calls.length <= done) await tick();
    await waitFor(() => expect(calls.length).toBeGreaterThan(done));
    const call = calls[done++];
    await act(async () => call.answer(answer(call.after)));
    return call.after;
  };
}
const mineNote = (over: Record<string, unknown> = {}) =>
  note("m", { author: "peer", mine: true, text: "MY-NOTE", created_at: nowS(), ...over });
const fakeTicks = async (body: () => Promise<void>) => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  try {
    await body();
  } finally {
    vi.useRealTimers();
  }
};
const rows = () => screen.queryAllByTestId("note");

test("(a) a delayed add answer cannot bring back a note whose removal the thread consumed", () =>
  fakeTicks(async () => {
    const saved = mineNote();
    const calls = heldReads();
    const read = reader(calls);
    const add = deferred<unknown>();
    api.addFederationRemoteNote.mockReturnValue(add.promise);
    render(<Thread side="peer" grantId={GRANT} />);
    await read(() => changes(0, []));
    fireEvent.change(screen.getByTestId("notes-box"), { target: { value: "MY-NOTE" } });
    await userEvent.click(screen.getByTestId("notes-send")); // commits at 1 on the owner; its answer waits
    await read(() => changes(1, [{ seq: 1, note_id: saved.note_id, note: saved }])); // the poll reads it
    expect(rows()).toHaveLength(1);
    expect(screen.queryByTestId("note-pending")).toBeNull();
    expect(await read(() => changes(2, [{ seq: 2, note_id: saved.note_id, removed: true }]))).toBe(1); // hidden
    expect(rows()).toHaveLength(0);
    // The old add answer arrives now.
    await act(async () => add.resolve({ note: saved, seq: 1 }));
    expect(rows()).toHaveLength(0);
    expect(screen.queryByText("MY-NOTE")).toBeNull();
    // The next read asks after 2 and gets nothing: still no row.
    expect(await read(() => changes(2, []))).toBe(2);
    expect(await read(() => changes(2, []))).toBe(2);
    expect(rows()).toHaveLength(0);
    expect(screen.queryByText("MY-NOTE")).toBeNull();
    expect(screen.queryByTestId("note-pending")).toBeNull();
    expect(api.addFederationRemoteNote).toHaveBeenCalledTimes(1);
  }));

test.each(["its removal", "a newer edit"] as const)(
  "(b) a delayed edit answer arriving after the thread read %s does not put its text back",
  (what) =>
    fakeTicks(async () => {
      const mine = mineNote();
      const calls = heldReads();
      const read = reader(calls);
      const edit = deferred<unknown>();
      Object.assign(api, { editFederationRemoteNote: vi.fn(() => edit.promise) });
      render(<Thread side="peer" grantId={GRANT} />);
      await read(() => changes(1, [{ seq: 1, note_id: mine.note_id, note: mine }]));
      const shown = await screen.findByTestId("note");
      await userEvent.click(within(shown).getByTestId("note-edit"));
      fireEvent.change(within(shown).getByTestId("note-edit-box"), { target: { value: "OLD-EDIT" } });
      await userEvent.click(within(shown).getByTestId("note-save")); // commits at 2; its answer waits
      const newer = { ...mine, text: "NEWEST-EDIT", edited_at: nowS() };
      await read(() =>
        what === "its removal"
          ? changes(3, [{ seq: 3, note_id: mine.note_id, removed: true }])
          : changes(3, [{ seq: 3, note_id: mine.note_id, note: newer }]),
      );
      await act(async () => edit.resolve({ note: { ...mine, text: "OLD-EDIT", edited_at: nowS() } }));
      await read(() => changes(3, []));
      await read(() => changes(3, []));
      expect(screen.queryByText("OLD-EDIT")).toBeNull();
      if (what === "its removal") expect(rows()).toHaveLength(0);
      else expect(within(screen.getByTestId("note")).getByTestId("note-text")).toHaveTextContent("NEWEST-EDIT");
      expect(screen.queryByTestId("notes-error")).toBeNull();
    }),
);

test("(c) a delayed delete answer after the thread consumed that removal resurrects nothing and is no error", () =>
  fakeTicks(async () => {
    const mine = mineNote();
    const calls = heldReads();
    const read = reader(calls);
    const del = deferred<unknown>();
    Object.assign(api, { deleteFederationRemoteNote: vi.fn(() => del.promise) });
    render(<Thread side="peer" grantId={GRANT} />);
    await read(() => changes(1, [{ seq: 1, note_id: mine.note_id, note: mine }]));
    await userEvent.click(within(await screen.findByTestId("note")).getByTestId("note-delete"));
    await read(() => changes(3, [{ seq: 3, note_id: mine.note_id, removed: true }])); // evicted meanwhile
    expect(rows()).toHaveLength(0);
    await act(async () => del.resolve({ note: { ...mine, text: "", deleted: true } }));
    await read(() => changes(3, []));
    expect(rows()).toHaveLength(0); // no tombstone row put back
    expect(screen.queryByTestId("notes-error")).toBeNull();
    expect(screen.queryByTestId("notes-refusal")).toBeNull();
  }));

test("(d) the answer first, then the read at the same change: exactly one row, no duplicate, no pending", () =>
  fakeTicks(async () => {
    const saved = mineNote();
    const calls = heldReads();
    const read = reader(calls);
    api.addFederationRemoteNote.mockResolvedValue({ note: saved, seq: 1 });
    render(<Thread side="peer" grantId={GRANT} />);
    await read(() => changes(0, []));
    fireEvent.change(screen.getByTestId("notes-box"), { target: { value: "MY-NOTE" } });
    await userEvent.click(screen.getByTestId("notes-send"));
    await waitFor(() => expect(rows()).toHaveLength(1)); // shown at once, from the answer
    expect(screen.queryByTestId("note-pending")).toBeNull();
    expect(screen.getByTestId("notes-box")).toHaveValue("");
    // The cursor did not move: the read is asked from 0 and carries the same note.
    expect(await read(() => changes(1, [{ seq: 1, note_id: saved.note_id, note: saved }]))).toBe(0);
    expect(await read(() => changes(1, []))).toBe(1);
    expect(rows()).toHaveLength(1);
  }));

/** The drawer under a parent that can close and reopen it, as the panes do: the thread's state
 *  outlives the drawer. */
function Closable({ grantId }: { grantId: string }) {
  const [notes, dispatch] = useReducer(notesReduce, notesInit);
  const [open, setOpen] = useReducer((o: boolean) => !o, true);
  return (
    <>
      <button type="button" data-testid="toggle-drawer" onClick={setOpen}>
        toggle
      </button>
      {open && (
        <NotesDrawer
          side="peer"
          grantId={grantId}
          peerId={GPU}
          other="gpu-box"
          thread={threadOf(notes, grantId)}
          dispatch={dispatch}
          epoch={0}
          stillCurrent={() => true}
          anchor={null}
          onClose={vi.fn()}
        />
      )}
    </>
  );
}

test.each(["nothing read yet", "the add and its removal read"] as const)(
  "(e) an add answer that lands while the drawer is closed (%s) is settled on reopen, and nothing is resurrected",
  (before) =>
    fakeTicks(async () => {
      const saved = mineNote();
      const calls = heldReads();
      const read = reader(calls);
      const add = deferred<unknown>();
      api.addFederationRemoteNote.mockReturnValue(add.promise);
      render(<Closable grantId={GRANT} />);
      await read(() => changes(0, []));
      fireEvent.change(screen.getByTestId("notes-box"), { target: { value: "MY-NOTE" } });
      await userEvent.click(screen.getByTestId("notes-send"));
      const hid = before === "the add and its removal read";
      if (hid) {
        await read(() => changes(1, [{ seq: 1, note_id: saved.note_id, note: saved }]));
        await read(() => changes(2, [{ seq: 2, note_id: saved.note_id, removed: true }]));
      }
      await userEvent.click(screen.getByTestId("toggle-drawer")); // closed mid-write
      expect(screen.queryByTestId("notes-drawer")).toBeNull();
      await act(async () => add.resolve({ note: saved, seq: 1 })); // the answer outlives the drawer
      await userEvent.click(screen.getByTestId("toggle-drawer")); // reopened
      await screen.findByTestId("notes-drawer");
      const asked = await read((after) => (after === 0 ? changes(1, [{ seq: 1, note_id: saved.note_id, note: saved }]) : changes(2, [])));
      expect(asked).toBe(hid ? 2 : 0);
      await read((after) => changes(Number(after) || 1, []));
      // Written once and not lost; or hidden and not brought back.
      expect(rows()).toHaveLength(hid ? 0 : 1);
      expect(screen.queryByTestId("note-pending")).toBeNull();
      expect(screen.getByTestId("notes-box")).toHaveValue("");
      expect(api.addFederationRemoteNote).toHaveBeenCalledTimes(1);
    }),
);

test("(f) the owner's own add answers with no change number: nothing is inserted from it, the read brings the note once", async () => {
  const saved = note("o", { author: "owner", mine: true, text: "OWNER-NOTE", created_at: nowS() });
  const threads = [deferred<unknown>(), deferred<unknown>()];
  let n = 0;
  api.federationNotes.mockImplementation(() => (threads[n++] ?? deferred<unknown>()).promise);
  Object.assign(api, { addFederationNote: vi.fn().mockResolvedValue({ note: saved }) });
  render(<Thread side="owner" grantId={GRANT} />);
  await act(async () => threads[0].resolve({ grant: {}, ended: false, notes: [], seq: 0, unread: 0 }));
  fireEvent.change(screen.getByTestId("notes-box"), { target: { value: "OWNER-NOTE" } });
  await userEvent.click(screen.getByTestId("notes-send"));
  // Saved, and waiting for the read: a row that says so, not a note put in from the answer.
  await waitFor(() => expect(screen.getByTestId("note-pending")).toHaveAttribute("data-state", "saved"));
  expect(rows()).toHaveLength(0);
  expect(screen.getByTestId("notes-box")).toHaveValue("");
  await waitFor(() => expect(api.federationNotes).toHaveBeenCalledTimes(2)); // read again at once
  await act(async () => threads[1].resolve({ grant: {}, ended: false, notes: [saved], seq: 1, unread: 0 }));
  expect(rows()).toHaveLength(1);
  expect(screen.queryByTestId("note-pending")).toBeNull();
});

test("(f) a status read that still says pending, landing after Withdraw's own answer, does not bring the request back", () =>
  fakeTicks(async () => {
    const button = await waiting();
    const id = "r".repeat(22);
    const withdrawn = deferred<unknown>();
    const late = deferred<unknown>();
    api.withdrawFederationRemoteAccess.mockReturnValue(withdrawn.promise);
    await userEvent.click(button); // Withdraw starts…
    const reads = api.federationRemoteAccess.mock.calls.length;
    api.federationRemoteAccess.mockReturnValue(late.promise);
    await act(async () => void vi.advanceTimersByTime(5_000)); // …then a poll read starts, cut before it
    await waitFor(() => expect(api.federationRemoteAccess.mock.calls.length).toBeGreaterThan(reads));
    await act(async () => withdrawn.resolve({ level: "view", request: { request_id: id, level: "send", state: "withdrawn" } }));
    await waitFor(() => expect(screen.queryByTestId("remote-access")).toBeNull());
    await act(async () => late.resolve({ level: "view", request: { request_id: id, level: "send", state: "pending" } }));
    await act(async () => {});
    expect(screen.queryByTestId("remote-access")).toBeNull(); // not "Request sent · waiting" again
    expect(screen.queryByTestId("remote-access-withdraw")).toBeNull();
  }));

test.each(["edit", "delete"] as const)("a note %s with a lost answer claims nothing and reads the thread again", async (what) => {
  const mine = note("m", { author: "peer", mine: true });
  api.federationRemoteNotes.mockResolvedValue({
    reset: false,
    seq: 1,
    changes: [{ seq: 1, note_id: mine.note_id, note: mine }],
    more: false,
    next: null,
    unread: 0,
  });
  const write = vi.fn().mockRejectedValue(LOST());
  Object.assign(api, { editFederationRemoteNote: write, deleteFederationRemoteNote: write });
  render(<Thread side="peer" grantId={GRANT} />);
  const shown = await screen.findByTestId("note");
  const reads = api.federationRemoteNotes.mock.calls.length;
  await userEvent.click(within(shown).getByTestId(`note-${what}`));
  if (what === "edit") {
    fireEvent.change(within(shown).getByTestId("note-edit-box"), { target: { value: "changed" } });
    await userEvent.click(within(shown).getByTestId("note-save"));
  }
  expect(await screen.findByTestId("notes-error")).toHaveTextContent(
    "Could not confirm that change. The thread shows what the other side has.",
  );
  await waitFor(() => expect(api.federationRemoteNotes.mock.calls.length).toBeGreaterThan(reads));
  expect(write).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("note")).toBeInTheDocument(); // still what the last read said
});

test.each(Object.keys(UNKNOWN) as (keyof typeof UNKNOWN)[])(
  "an access request answered with %s says neither sent nor not sent, and the status is read again",
  async (name) => {
    await pane();
    await userEvent.click((await screen.findAllByTestId("remote-access-ask"))[0]);
    const dialog = await screen.findByTestId("ask-access");
    const reads = api.federationRemoteAccess.mock.calls.length;
    api.askFederationRemoteAccess.mockRejectedValueOnce(UNKNOWN[name]());
    await userEvent.click(within(dialog).getByTestId("ask-access-go"));
    const error = await within(dialog).findByTestId("ask-access-error");
    expect(error).toHaveAttribute("data-failure", "unconfirmed");
    expect(error).toHaveTextContent("Could not confirm that the request arrived.");
    expect(error).not.toHaveTextContent(/Nothing was sent|not sent|Request sent/i);
    await waitFor(() => expect(api.federationRemoteAccess.mock.calls.length).toBeGreaterThan(reads));
    expect(screen.queryByTestId("remote-access")).toBeNull(); // the owner reports none: no "Request sent"
    expect(api.askFederationRemoteAccess).toHaveBeenCalledTimes(1);
  },
);

test("an access request whose answer was lost but that arrived: the status read shows it waiting and the dialog closes", async () => {
  await pane();
  await userEvent.click((await screen.findAllByTestId("remote-access-ask"))[0]);
  const dialog = await screen.findByTestId("ask-access");
  api.askFederationRemoteAccess.mockRejectedValueOnce(LOST());
  api.federationRemoteAccess.mockResolvedValue({
    level: "view",
    request: { request_id: "r".repeat(22), level: "suggest", state: "pending" },
  });
  await userEvent.click(within(dialog).getByTestId("ask-access-go"));
  expect(await screen.findByTestId("remote-access-text")).toHaveTextContent("Request sent · waiting for gpu-box's operator");
  await waitFor(() => expect(screen.queryByTestId("ask-access")).toBeNull());
  expect(api.askFederationRemoteAccess).toHaveBeenCalledTimes(1);
});

test.each(Object.keys(UNKNOWN) as (keyof typeof UNKNOWN)[])(
  "a Withdraw answered with %s is 'could not confirm', re-reads the status, and is not sent again",
  async (name) => {
    const button = await waiting();
    const reads = api.federationRemoteAccess.mock.calls.length;
    api.withdrawFederationRemoteAccess.mockRejectedValue(UNKNOWN[name]());
    await userEvent.click(button);
    const error = await screen.findByTestId("remote-access-error");
    expect(error).toHaveTextContent("Could not confirm that the request was withdrawn.");
    expect(error).not.toHaveTextContent("Not withdrawn");
    await waitFor(() => expect(api.federationRemoteAccess.mock.calls.length).toBeGreaterThan(reads));
    expect(api.withdrawFederationRemoteAccess).toHaveBeenCalledTimes(1);
  },
);

test("after not_pending the pane reads the snapshot again; the next answer names the fresh offer under a new key", async () => {
  const card = await sendPane([REQUEST]);
  // The owner replaced the request under the same id: what waits now has another offer.
  api.federationRemoteSnapshot.mockResolvedValue(
    snapshot({ pending_requests: [{ ...REQUEST, offer: FRESH_OFFER, summary: "npm run build" }] }),
  );
  const reads = api.federationRemoteSnapshot.mock.calls.length;
  api.federationRemoteDecide.mockRejectedValueOnce(
    new ApiError(409, "that request is no longer waiting for an answer", { code: "not_pending" }),
  );
  api.federationRemoteDecide.mockResolvedValueOnce({ outcome: "accepted", author: { via: "peer", name: "studio" } });
  await userEvent.click(within(card).getByRole("button", { name: "Approve once" }));
  // Read again, by itself, exactly once; and NOT answered again by itself.
  await waitFor(() => expect(api.federationRemoteSnapshot.mock.calls.length).toBe(reads + 1));
  const now = await screen.findByText("npm run build");
  const fresh = now.closest('[data-testid="peer-decision"]') as HTMLElement;
  expect(api.federationRemoteDecide).toHaveBeenCalledTimes(1);
  expect(decideBody(0)).toMatchObject({ request_id: "req-1", offer: OFFER });
  // The refusal was about the OTHER request: this card starts clean, with its controls.
  expect(within(fresh).queryByTestId("peer-decision-outcome")).toBeNull();
  await userEvent.click(within(fresh).getByRole("button", { name: "Approve once" }));
  await waitFor(() => expect(api.federationRemoteDecide).toHaveBeenCalledTimes(2));
  expect(decideBody(1)).toMatchObject({ request_id: "req-1", offer: FRESH_OFFER, choice: "approve" });
  expect(decideBody(1).client_key).toMatch(UUID_RE);
  expect(decideBody(1).client_key).not.toBe(decideBody(0).client_key);
  expect(api.federationRemoteSnapshot.mock.calls.length).toBe(reads + 1);
});

test("not_pending with the same offer still shown: said in the card, and the next press is a new key", async () => {
  const card = await sendPane([REQUEST]);
  api.federationRemoteDecide.mockRejectedValue(
    new ApiError(409, "that request is no longer waiting for an answer", { code: "not_pending" }),
  );
  await userEvent.click(within(card).getByRole("button", { name: "Approve once" }));
  expect(await within(card).findByTestId("peer-decision-outcome")).toHaveAttribute("data-outcome", "not_pending");
  await userEvent.click(within(card).getByRole("button", { name: "Approve once" }));
  await waitFor(() => expect(api.federationRemoteDecide).toHaveBeenCalledTimes(2));
  expect(decideBody(1).offer).toBe(OFFER);
  expect(decideBody(1).client_key).not.toBe(decideBody(0).client_key);
});

test.each([
  ["no offer", undefined],
  ["an offer one character short", OFFER.slice(1)],
  ["an offer with a character outside the alphabet", `${OFFER.slice(0, 31)}=`],
  ["an offer that is not a string", 7],
])("a send peer's request with choices and %s has no answer control, and nothing is sent", async (_name, offer) => {
  const card = await sendPane([{ ...REQUEST, offer }]);
  expect(card).toHaveAttribute("data-role", "watch");
  expect(within(card).queryAllByRole("button")).toHaveLength(0);
  expect(within(card).getByTestId("peer-decision-line")).toHaveTextContent("Waiting for an answer.");
  expect(api.federationRemoteDecide).not.toHaveBeenCalled();
});

// ---- a note the owner hid: a repeated add finds nothing, and is not sent again -----------------

test("a Retry of a note the owner hid says the note is gone: no loop, no second note, the thread stays", async () => {
  api.federationRemoteNotes.mockResolvedValue({
    reset: false,
    seq: 1,
    changes: [{ seq: 1, note_id: note("a").note_id, note: note("a") }],
    more: false,
    next: null,
    unread: 0,
  });
  render(<Thread side="peer" grantId={GRANT} />);
  expect(await screen.findByText("note a")).toBeInTheDocument();
  // The first delivery was saved but its answer was lost; the owner hid the note meanwhile.
  api.addFederationRemoteNote.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  api.addFederationRemoteNote.mockRejectedValue(new ApiError(404, "no such suggestion or note", { code: "not_found" }));
  await userEvent.type(screen.getByTestId("notes-box"), "about the lock");
  await userEvent.click(screen.getByTestId("notes-send"));
  await userEvent.click(await screen.findByRole("button", { name: /Retry/ }));
  expect(await screen.findByTestId("notes-refusal")).toHaveTextContent("That note is no longer in this thread. Nothing was sent again.");
  expect(api.addFederationRemoteNote).toHaveBeenCalledTimes(2);
  expect(api.addFederationRemoteNote.mock.calls[1][2]).toEqual(api.addFederationRemoteNote.mock.calls[0][2]);
  // Not the share's end: the thread is still there. Nothing pending, nothing left to resend.
  expect(screen.getByText("note a")).toBeInTheDocument();
  expect(screen.queryByTestId("notes-none")).toBeNull();
  expect(screen.queryByRole("button", { name: /Retry/ })).toBeNull();
  expect(screen.getByTestId("notes-box")).toHaveValue("");
  expect(screen.getByTestId("notes-send")).toBeDisabled();
  await act(async () => {
    await new Promise((r) => setTimeout(r, 50));
  });
  expect(api.addFederationRemoteNote).toHaveBeenCalledTimes(2);
});

test("under StrictMode the access read fills the foot, and Ask for access opens from it", async () => {
  api.federationRemoteAccess.mockResolvedValue({
    level: "view",
    request: { request_id: "r".repeat(22), level: "send", state: "declined" },
  });
  await pane(true);
  expect(await screen.findByTestId("remote-access-text")).toHaveTextContent("gpu-box's operator declined.");
});

test("under StrictMode the ask-access dialog shows its refusal, and the request was sent once", async () => {
  const onAnswer = vi.fn();
  const onClose = vi.fn();
  api.askFederationRemoteAccess.mockRejectedValue(new ApiError(422, "too many levels", { code: "invalid" }));
  render(
    <StrictMode>
      <AskAccessDialog peerId={GPU} grantId={GRANT} owner="gpu-box" title="Tune" level="view" onClose={onClose} onAnswer={onAnswer} />
    </StrictMode>,
  );
  await userEvent.click(screen.getByTestId("ask-access-go"));
  expect(await screen.findByTestId("ask-access-error")).toHaveTextContent("too many levels Nothing was sent.");
  expect(api.askFederationRemoteAccess).toHaveBeenCalledTimes(1);
  expect(onAnswer).toHaveBeenCalledWith({ ok: false, failure: "rejected" });
  expect(onClose).not.toHaveBeenCalled();
  // and an accepted one closes the dialog: the answer was not dropped by the replay
  api.askFederationRemoteAccess.mockResolvedValue({ request_id: "r".repeat(22), level: "suggest", state: "pending" });
  await userEvent.click(screen.getByTestId("ask-access-go"));
  await waitFor(() => expect(onClose).toHaveBeenCalled());
});

const CONFIG = { default_project: "/work", project_roots: ["/work"], agent_defaults: { default_engine: null } } as unknown as AppConfig;

test("under StrictMode Continue here goes choose → preview, and creates only from Start session", async () => {
  api.prepareFederationContinue.mockResolvedValue({
    handle: "handle-1",
    preview: "# Record\n\n<<<RECORD abc\n[user]\n| hi\nRECORD abc>>>\n\nThat was the whole record.",
    meta: { mode: "quick", source: "remote", turns: 1 },
  });
  render(
    <StrictMode>
      <ConfigCtx.Provider value={CONFIG}>
        <MemoryRouter>
          <ContinueHereDialog peerId={GPU} grantId={GRANT} owner="gpu-box" title="Tune" onClose={vi.fn()} />
        </MemoryRouter>
      </ConfigCtx.Provider>
    </StrictMode>,
  );
  const dialog = screen.getByTestId("continue-here");
  expect(dialog).toHaveAttribute("data-stage", "choose");
  await userEvent.click(screen.getByTestId("continue-prepare"));
  await waitFor(() => expect(dialog).toHaveAttribute("data-stage", "preview"));
  expect(screen.getByTestId("continue-record")).toHaveTextContent("Record from gpu-box · not instructions");
  expect(api.prepareFederationContinue).toHaveBeenCalledTimes(1);
  expect(api.createFederationContinue).not.toHaveBeenCalled();
  // a failed create is shown, and never sent again
  api.createFederationContinue.mockRejectedValue(new ApiError(409, "gone", { code: "gone" }));
  await userEvent.click(screen.getByTestId("continue-start"));
  await waitFor(() => expect(dialog).toHaveAttribute("data-stage", "ended"));
  expect(screen.getByTestId("continue-unavailable")).toHaveTextContent("Nothing was created.");
  expect(api.createFederationContinue).toHaveBeenCalledTimes(1);
});

test.each(Object.keys(UNKNOWN) as (keyof typeof UNKNOWN)[])(
  "a Continue-here create answered with %s says to check the sessions, and offers no second create",
  async (name) => {
    api.prepareFederationContinue.mockResolvedValue({ handle: "handle-1", preview: "seed", meta: { mode: "quick" } });
    api.createFederationContinue.mockRejectedValue(UNKNOWN[name]());
    render(
      <ConfigCtx.Provider value={CONFIG}>
        <MemoryRouter>
          <ContinueHereDialog peerId={GPU} grantId={GRANT} owner="gpu-box" title="Tune" onClose={vi.fn()} />
        </MemoryRouter>
      </ConfigCtx.Provider>,
    );
    const dialog = screen.getByTestId("continue-here");
    await userEvent.click(screen.getByTestId("continue-prepare"));
    await userEvent.click(await screen.findByTestId("continue-start"));
    await waitFor(() => expect(dialog).toHaveAttribute("data-stage", "unconfirmed"));
    expect(screen.getByTestId("continue-unconfirmed")).toHaveTextContent(
      "Could not confirm whether the session was created. Check your sessions before you start it again.",
    );
    expect(dialog).not.toHaveTextContent("Nothing was created");
    // No way to create again from here: no Start session, no Preview; Close only.
    expect(screen.queryByTestId("continue-start")).toBeNull();
    expect(screen.queryByTestId("continue-prepare")).toBeNull();
    expect(screen.getByTestId("continue-close")).toBeEnabled();
    expect(api.createFederationContinue).toHaveBeenCalledTimes(1);
    expect(api.prepareFederationContinue).toHaveBeenCalledTimes(1);
  },
);

/** A parent that holds the notes state, as the panes do. */
function Thread({ side, grantId }: { side: "owner" | "peer"; grantId: string }) {
  const [notes, dispatch] = useReducer(notesReduce, notesInit);
  return (
    <NotesDrawer
      side={side}
      grantId={grantId}
      peerId={side === "peer" ? GPU : undefined}
      other="gpu-box"
      thread={threadOf(notes, grantId)}
      dispatch={dispatch}
      epoch={0}
      stillCurrent={() => true}
      anchor={null}
      onClose={vi.fn()}
    />
  );
}

const note = (id: string, over: Record<string, unknown> = {}) => ({
  note_id: id.padEnd(22, "x"),
  author: "owner",
  mine: false,
  actor: "",
  text: `note ${id}`,
  created_at: nowS() - 60,
  edited_at: null,
  deleted: false,
  ...over,
});

test.each(["owner", "peer"] as const)("under StrictMode the %s's notes drawer loads its thread", async (side) => {
  api.federationNotes.mockResolvedValue({ grant: {}, ended: false, notes: [note("a")], seq: 1, unread: 0 });
  api.federationRemoteNotes.mockResolvedValue({
    reset: false,
    seq: 1,
    changes: [{ seq: 1, note_id: note("a").note_id, note: note("a") }],
    more: false,
    next: null,
    unread: 0,
  });
  render(
    <StrictMode>
      <Thread side={side} grantId={GRANT} />
    </StrictMode>,
  );
  expect(await screen.findByText("note a")).toBeInTheDocument();
  expect(screen.queryByText("Loading notes…")).toBeNull();
});

// ---- a peer's numbers: never a throw, never a crash, never a loop -----------------------------

test("a notes thread whose times no Date can hold draws every note, at an unknown time", async () => {
  const notes = HOSTILE.map((v, i) => note(`n${i}`, { created_at: v, edited_at: v }));
  api.federationRemoteNotes.mockResolvedValue({
    reset: false,
    seq: 1e300, // not a cursor: never asked after, never marked read
    changes: notes.map((n, i) => ({ seq: i + 1, note_id: n.note_id, note: n })),
    more: true,
    next: null,
    unread: 0,
  });
  render(<Thread side="peer" grantId={GRANT} />);
  const rows = await screen.findAllByTestId("note");
  expect(rows).toHaveLength(HOSTILE.length);
  for (const r of rows) expect(within(r).getByTestId("note-time")).toHaveTextContent(UNKNOWN_TIME);
  // the hostile cursor drove no loop: one read per poll, after 0, and no read mark at 1e300
  expect(api.federationRemoteNotes.mock.calls.every((c) => c[2] === 0)).toBe(true);
  expect(api.federationRemoteNotes).toHaveBeenCalledTimes(1);
  expect(api.readFederationRemoteNotes).not.toHaveBeenCalled();
});

function strip(strict = false) {
  return render(
    wrap(
      strict,
      <MemoryRouter>
        <ShareStrip sessionKey={KEY} sharesOpen={false} anchor={null} onOpenShare={vi.fn()} />
      </MemoryRouter>,
    ),
  );
}

test("under StrictMode the owner's share strip reads its grants and shows who is watching", async () => {
  api.federationGrants.mockResolvedValue({ grants: [grantRow({ watching: true })], generation: 1 });
  strip(true);
  expect(await screen.findByTestId("share-strip")).toHaveTextContent("Shared with");
  expect(screen.getByTestId("strip-watching")).toHaveTextContent("gpu-box is watching");
});

test("the strip with hostile counts and expiries: every row drawn, no number of notes, an unknown date", async () => {
  api.federationGrants.mockResolvedValue({
    grants: HOSTILE.map((v, i) => grantRow({ grant_id: `g${i}`.padEnd(22, "x"), peer_name: `peer${i}`, notes: v, expires_at: v })),
    generation: 1,
  });
  strip();
  const box = await screen.findByTestId("share-strip");
  await userEvent.click(within(box).getByTestId("strip-notes-pick"));
  const picker = await screen.findByTestId("strip-notes-picker");
  expect(within(picker).getAllByRole("menuitem")).toHaveLength(HOSTILE.length);
  expect(within(picker).getAllByText("no notes")).toHaveLength(HOSTILE.length);
  await userEvent.click(within(box).getByTestId("strip-toggle"));
  expect(within(box).getAllByTestId("strip-row")).toHaveLength(HOSTILE.length);
  for (const r of within(box).getAllByTestId("strip-row")) expect(r).toHaveTextContent(`until ${UNKNOWN_DATE}`);
});

test("a terminal grant never has a strip row; an API grant of the same session does", async () => {
  api.federationGrants.mockResolvedValue({ grants: [grantRow({ kind: "terminal", watching: true })], generation: 1 });
  const { unmount } = strip();
  await waitFor(() => expect(api.federationGrants).toHaveBeenCalled());
  await act(async () => {});
  expect(screen.queryByTestId("share-strip")).toBeNull();
  unmount();
  api.federationGrants.mockResolvedValue({
    grants: [grantRow({ kind: "terminal" }), grantRow({ grant_id: GRANT_B, kind: "api" })],
    generation: 1,
  });
  strip();
  const box = await screen.findByTestId("share-strip");
  expect(within(box).getAllByTestId("strip-row").map((r) => r.dataset.grant)).toEqual([GRANT_B]);
});

test("nothing typed and nothing received is written to browser storage", async () => {
  api.federationRemoteNotes.mockResolvedValue({
    reset: false,
    seq: 1,
    changes: [{ seq: 1, note_id: note("s").note_id, note: note("s", { text: "SECRET-NOTE" }) }],
    more: false,
    next: null,
    unread: 0,
  });
  render(<Thread side="peer" grantId={GRANT} />);
  await screen.findByText("SECRET-NOTE");
  fireEvent.change(screen.getByTestId("notes-box"), { target: { value: "SECRET-DRAFT" } });
  const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
  expect(stored).not.toContain("SECRET");
});
