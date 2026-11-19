import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ApiError } from "../../lib/api";
import { REMOTE_ROUTE, UNKNOWN_TIME, pendingOps, remotePath } from "../../lib/federation";

// The receiving side of suggest and send (#2061) in jsdom, for what the browser spec cannot
// plant: a peer's numbers that JSON cannot carry (NaN, Infinity), StrictMode's effect replay
// (main.tsx mounts the app under <StrictMode>), and a terminal row that claims a level that
// writes. The journeys are e2e/peer-input.spec.ts's.
const api = vi.hoisted(() => ({
  federationRemote: vi.fn(),
  federationRemoteSnapshot: vi.fn(),
  federationRemoteEvents: vi.fn(),
  federationRemoteHistory: vi.fn(),
  federationRemoteFrame: vi.fn(),
  federationRemoteSuggestions: vi.fn(),
  federationRemoteOperation: vi.fn(),
  federationRemoteSuggest: vi.fn(),
  federationRemoteSend: vi.fn(),
  federationRemoteWithdraw: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  api,
}));

const { RemotePane } = await import("./RemotePane");
const { RemoteSessionView } = await import("./RemoteSessionView");
const { refreshRemoteShares, resetRemoteSharesForTest } = await import("../../app/remoteShares");

const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const GRANT = "g7Jx9aBcDeFgHiJkLmNoPq";
const KEY_A = "0b9e3c1a-7f52-4c1d-9a6e-3d2f1b0a9c8d";
const nowS = () => Date.now() / 1000;

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
  level: "suggest",
  expires_at: nowS() + 7 * 86400,
  revision: 3,
  seen_at: nowS() - 30,
  ...over,
});

const own = (id: string, created_at: unknown, state = "pending") => ({
  suggestion_id: id.padEnd(22, "x"),
  client_key: "",
  state,
  handoff: null,
  created_at,
  updated_at: created_at,
});

const held = (...args: unknown[]) => {
  const signal = args[args.length - 1] as AbortSignal | undefined;
  return new Promise((_resolve, reject) => signal?.addEventListener?.("abort", () => reject(new Error("aborted"))));
};

beforeEach(() => {
  resetRemoteSharesForTest();
  sessionStorage.clear();
  Object.values(api).forEach((f) => f.mockReset());
  api.federationRemote.mockResolvedValue({ remote: [row()] });
  api.federationRemoteSnapshot.mockResolvedValue({
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
  });
  api.federationRemoteEvents.mockImplementation(held);
  api.federationRemoteHistory.mockImplementation(() => new Promise(() => {}));
  api.federationRemoteFrame.mockImplementation(held);
  api.federationRemoteSuggestions.mockResolvedValue({ suggestions: [] });
  api.federationRemoteOperation.mockResolvedValue({ known: false });
});

async function pane(strict: boolean) {
  await refreshRemoteShares(); // the mirror lists the share before the pane opens, as in the app
  const tree = (
    <MemoryRouter>
      <RemotePane peerId={GPU} grantId={GRANT} />
    </MemoryRouter>
  );
  render(strict ? <StrictMode>{tree}</StrictMode> : tree);
  await screen.findByText("One lock for the whole batch.");
}

// ---- StrictMode: every loader of the receiving side -----------------------------------------

test("under StrictMode, Your suggestions is read and shown", async () => {
  api.federationRemoteSuggestions.mockResolvedValue({
    suggestions: [own("mine1", nowS() - 60), own("mine2", nowS() - 30, "approved")],
  });
  await pane(true);
  const list = await screen.findByTestId("remote-suggestions");
  const rows = within(list).getAllByTestId("remote-suggestion");
  expect(rows.map((r) => r.dataset.state).sort()).toEqual(["approved", "pending"]);
  expect(within(list).getByText("waiting for gpu-box's operator")).toBeInTheDocument();
  // the composer of the mirrored level, enabled: the replay did not leave the pane dead
  expect(screen.getByTestId("remote-composer")).toHaveAttribute("data-kind", "suggest");
  expect(screen.getByTestId("remote-composer-text")).toBeEnabled();
});

test("under StrictMode, a request left over from before the reload is asked about and shown", async () => {
  pendingOps.add({ peer_id: GPU, grant_id: GRANT, client_key: KEY_A, kind: "send", started_at: Date.now() - 2_000 });
  api.federationRemote.mockResolvedValue({ remote: [row({ level: "send" })] });
  api.federationRemoteOperation.mockResolvedValue({ known: true, kind: "send", handoff: "queued" });
  await pane(true);
  const delivery = await screen.findByTestId("remote-delivery");
  expect(delivery).toHaveAttribute("data-delivery", "queued");
  expect(api.federationRemoteOperation).toHaveBeenCalledWith(GPU, GRANT, KEY_A, expect.anything());
  // known: the content-free record is gone, and nothing was sent again
  expect(pendingOps.of(GPU, GRANT)).toEqual([]);
  expect(api.federationRemoteSend).not.toHaveBeenCalled();
  expect(api.federationRemoteSuggest).not.toHaveBeenCalled();
  expect(screen.queryByTestId("remote-reload-notice")).toBeNull();
});

test("under StrictMode, an unknown answer is the may-or-may-not notice and the record stays", async () => {
  pendingOps.add({ peer_id: GPU, grant_id: GRANT, client_key: KEY_A, kind: "suggest", started_at: Date.now() - 2_000 });
  await pane(true);
  expect(await screen.findByTestId("remote-reload-notice")).toHaveTextContent("may or may not have arrived");
  expect(pendingOps.of(GPU, GRANT).map((o) => o.client_key)).toEqual([KEY_A]);
  expect(api.federationRemoteSuggest).not.toHaveBeenCalled();
});

// ---- an answer lost with the channel is not a refusal (#2061) --------------------------------

const failure = (status: number, code: string) => new ApiError(status, code, { code, detail: code });
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
};

async function send(text: string) {
  fireEvent.change(screen.getByTestId("remote-composer-text"), { target: { value: text } });
  fireEvent.click(screen.getByTestId("remote-composer-go"));
}

// The owner delivered it, and its answer did not come back: the channel ended, or the owner
// withheld the answer after the effect (`504 peer_lost` either way); or the owner is a build
// behind and said `busy` after delivering, its byte budget for answers spent (`429 busy`).
test.each([
  [504, "peer_lost"],
  [429, "busy"],
])("a reply lost after the delivery (%i %s): not confirmed, the record kept, and a reload asks the ORIGINAL key", async (status, code) => {
  api.federationRemote.mockResolvedValue({ remote: [row({ level: "send" })] });
  api.federationRemoteSend.mockRejectedValue(failure(status, code));
  await pane(false);
  await send("Deploy it exactly once.");
  const delivery = await screen.findByTestId("remote-delivery");
  expect(delivery).toHaveAttribute("data-delivery", "unconfirmed");
  expect(within(delivery).queryByRole("button")).toBeNull(); // no resend control
  expect(screen.queryByTestId("remote-composer-error")).toBeNull(); // never "nothing was sent"
  const [, , sentBody] = api.federationRemoteSend.mock.calls[0];
  const key = (sentBody as { client_key: string }).client_key;
  // the content-free record is still there: this is what lets a reload ask
  expect(pendingOps.of(GPU, GRANT).map((o) => o.client_key)).toEqual([key]);
  expect(JSON.stringify(sessionStorage)).not.toContain("Deploy it");

  // ── reload ──
  cleanup();
  const first = deferred<unknown>();
  const second = deferred<unknown>();
  api.federationRemoteOperation.mockReset();
  api.federationRemoteOperation.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  await pane(false);
  await waitFor(() => expect(api.federationRemoteOperation).toHaveBeenCalledTimes(1));
  expect(api.federationRemoteOperation).toHaveBeenCalledWith(GPU, GRANT, key, expect.anything());
  // the owner does not know the key yet: the uncertainty is said, and the record stays
  first.resolve({ known: false });
  expect(await screen.findByTestId("remote-reload-notice")).toHaveTextContent("may or may not have arrived");
  expect(pendingOps.of(GPU, GRANT).map((o) => o.client_key)).toEqual([key]);
  // asked again (5 s later), the same key; now the owner says what became of it
  await waitFor(() => expect(api.federationRemoteOperation).toHaveBeenCalledTimes(2), { timeout: 8_000 });
  expect(api.federationRemoteOperation.mock.calls[1][2]).toBe(key);
  expect(screen.getByTestId("remote-reload-notice")).toBeInTheDocument(); // until it answers
  second.resolve({ known: true, kind: "send", handoff: "sent" });
  await waitFor(() => expect(screen.getByTestId("remote-delivery")).toHaveAttribute("data-delivery", "sent"));
  expect(screen.queryByTestId("remote-reload-notice")).toBeNull();
  expect(pendingOps.of(GPU, GRANT)).toEqual([]);
  // ONE send in all: nothing was resubmitted, before or after the reload
  expect(api.federationRemoteSend).toHaveBeenCalledTimes(1);
  expect(api.federationRemoteSuggest).not.toHaveBeenCalled();
}, 15_000);

test("no channel at all (503 peer_offline) is a refusal: the record goes and the text stays", async () => {
  api.federationRemote.mockResolvedValue({ remote: [row({ level: "send" })] });
  api.federationRemoteSend.mockRejectedValue(failure(503, "peer_offline"));
  await pane(false);
  await send("Never left.");
  await waitFor(() => expect(api.federationRemoteSend).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(pendingOps.of(GPU, GRANT)).toEqual([]));
  expect(screen.getByTestId("remote-composer-text")).toHaveValue("Never left.");
});

// ---- not sent: the same guarded resend, whichever came first (#2061) -------------------------

const FIRST_TURN = {
  turn_id: "aaaaaaaa-0000-4000-8000-000000000001",
  kind: "operator",
  state: "completed",
  text: "Find out why the queue stalls.",
  reply: "One lock for the whole batch.",
};
const snapshotWith = (revision: number, ...mine: string[]) => ({
  link: "L".repeat(24),
  revision,
  event_cursor: revision,
  turns: [
    FIRST_TURN,
    ...mine.map((text, i) => ({
      turn_id: `bbbbbbbb-0000-4000-8000-00000000000${i + 2}`,
      kind: "operator",
      state: "queued",
      text,
      reply: null,
      mine: true,
    })),
  ],
  omitted_turns: 0,
  pending_requests: [],
  can_send: false,
});

test.each(["the reply first", "the snapshot first"])(
  "a message that was not sent offers Send again on its turn, with %s, and resends under the same key",
  async (order) => {
    api.federationRemote.mockResolvedValue({ remote: [row({ level: "send" })] });
    const moved = deferred<unknown>();
    api.federationRemoteEvents.mockImplementationOnce(() => moved.promise);
    const reply = deferred<unknown>();
    api.federationRemoteSend.mockReturnValueOnce(reply.promise);
    await pane(false);
    await send("Run the suite.");
    await waitFor(() => expect(screen.getByTestId("remote-delivery")).toHaveAttribute("data-delivery", "sending"));

    // The owner recorded the turn and could not hand it to the agent. Its conversation shows
    // the turn; its answer says `not_sent`. Either can reach this pane first.
    const answer = async () => {
      reply.resolve({ operation_id_known: true, handoff: "not_sent" });
      await waitFor(() => expect(screen.getByTestId("remote-delivery")).toHaveAttribute("data-delivery", "not_sent"));
    };
    const show = async () => {
      api.federationRemoteSnapshot.mockResolvedValue(snapshotWith(6, "Run the suite."));
      moved.resolve({ events: [{ kind: "turn" }], next_cursor: 6, revision: 6 });
      await waitFor(() => expect(document.querySelectorAll("[data-turn]")).toHaveLength(2));
    };
    if (order === "the reply first") {
      await answer();
      await show();
    } else {
      await show();
      await answer();
    }

    // The same outcome in both orders: the words once, under their turn, with the resend.
    await waitFor(() => expect(screen.queryByTestId("remote-own-message")).toBeNull());
    expect(screen.getAllByText("Run the suite.")).toHaveLength(1);
    const delivery = screen.getByTestId("remote-delivery");
    expect(delivery).toHaveAttribute("data-delivery", "not_sent");
    expect(delivery.closest("[data-turn]")).not.toBeNull();
    const again = within(delivery).getByRole("button", { name: "Send again" });

    // Send again: the SAME text under the SAME key (the owner answers that operation; a key
    // never makes a second turn), and nothing new is drawn.
    api.federationRemoteSend.mockResolvedValueOnce({ operation_id_known: true, handoff: "sent" });
    fireEvent.click(again);
    await waitFor(() => expect(screen.getByTestId("remote-delivery")).toHaveAttribute("data-delivery", "sent"));
    expect(api.federationRemoteSend).toHaveBeenCalledTimes(2);
    const [first, second] = api.federationRemoteSend.mock.calls.map((c) => c[2]);
    expect(second).toEqual(first);
    expect(first).toMatchObject({ text: "Run the suite." });
    expect(screen.queryByTestId("remote-own-message")).toBeNull();
    expect(screen.getAllByText("Run the suite.")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Send again" })).toBeNull();
  },
);

// ---- a peer's numbers -----------------------------------------------------------------------

const HOSTILE_TIMES: unknown[] = [1e300, Number.MAX_VALUE, Infinity, -Infinity, NaN, -1, 0, "14:05", null, {}];

test("Your suggestions with times no Date can hold: every row is drawn, as an unknown time", async () => {
  api.federationRemoteSuggestions.mockResolvedValue({
    suggestions: [
      ...HOSTILE_TIMES.map((t, i) => own(`bad${i}`, t)),
      own("good0", Date.UTC(2026, 10, 17, 14, 5) / 1000),
    ],
  });
  await pane(false);
  const list = await screen.findByTestId("remote-suggestions");
  const rows = within(list).getAllByTestId("remote-suggestion");
  expect(rows).toHaveLength(HOSTILE_TIMES.length + 1);
  const unknown = rows.filter((r) => r.textContent?.includes(`Suggested at ${UNKNOWN_TIME}`));
  expect(unknown).toHaveLength(HOSTILE_TIMES.length);
  // the real one is a time (in this browser's zone), not the neutral words
  expect(rows.filter((r) => /Suggested at \d\d:\d\d/.test(r.textContent ?? ""))).toHaveLength(1);
  // and the pane is still the pane: the conversation and the composer are there
  expect(screen.getByText("One lock for the whole batch.")).toBeInTheDocument();
  expect(screen.getByTestId("remote-composer")).toBeInTheDocument();
});

// ---- a terminal is never written to ---------------------------------------------------------

test("a row that says terminal gets no composer in the conversation pane, whatever level it claims", async () => {
  api.federationRemote.mockResolvedValue({ remote: [row({ kind: "terminal", level: "send" })] });
  await pane(false);
  expect(screen.queryByTestId("remote-composer")).toBeNull();
  expect(screen.getByTestId("remote-view-only")).toBeInTheDocument();
  expect(api.federationRemoteSuggestions).not.toHaveBeenCalled();
});

test.each(["suggest", "send"])(
  "a terminal share listed at %s opens the screen pane, which has no composer",
  async (level) => {
    api.federationRemote.mockResolvedValue({ remote: [row({ kind: "terminal", engine: "claude", level })] });
    await refreshRemoteShares();
    render(
      <MemoryRouter initialEntries={[remotePath(GPU, GRANT)]}>
        <Routes>
          <Route path={REMOTE_ROUTE} element={<RemoteSessionView />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(api.federationRemoteFrame).toHaveBeenCalled());
    expect(screen.getByTestId("remote-pane")).toHaveAttribute("aria-label", expect.stringContaining("screen only"));
    expect(screen.queryByTestId("remote-composer")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(api.federationRemoteSnapshot).not.toHaveBeenCalled();
    expect(api.federationRemoteSuggestions).not.toHaveBeenCalled();
    expect(api.federationRemoteSuggest).not.toHaveBeenCalled();
    expect(api.federationRemoteSend).not.toHaveBeenCalled();
  },
);
