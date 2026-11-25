import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { remotePath } from "../../lib/federation";
import { SESSIONS_PATH } from "../../lib/routes";

// Leaving a share (#2201): the ONE action, in the shared session's header (both panes) and in
// Settings → Peers. Every outcome class, one request per press, the state bound to a revision,
// a late answer against a newer list read, and Check again that reads and never writes. The
// journey in a real browser is e2e/share-leave.spec.ts.
const api = vi.hoisted(() => ({
  federationRemote: vi.fn(),
  federationRemoteLeave: vi.fn(),
  federationRemoteFrame: vi.fn(),
  federationGrantsToPeer: vi.fn(),
  revokeFederationGrant: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  api,
}));

const { ApiError } = await import("../../lib/api");
const { PeerShares } = await import("./PeerShares");
const { RemoteScreenPane } = await import("./RemoteScreenPane");
const { resetRemoteSharesForTest, useRemoteShares, refreshRemoteShares } = await import(
  "../../app/remoteShares"
);

const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const GRANT = "g7Jx9aBcDeFgHiJkLmNoPq";
const OTHER = "h8Ky0bCdEfGhIjKlMnOpQr";
const NOW = Date.now() / 1000;

const row = (over: Record<string, unknown> = {}) => ({
  key: `peer:${GPU}/codex-api:1`,
  peer_id: GPU,
  peer_name: "gpu-box",
  connected: true,
  grant_id: GRANT,
  engine: "codex-api",
  native_id: "1",
  title: "Migrate the billing tables",
  kind: "api",
  level: "view",
  expires_at: NOW + 6 * 86400,
  revision: 4,
  seen_at: NOW,
  ...over,
});
const second = row({ grant_id: OTHER, title: "Bench the new tokenizer", kind: "terminal", revision: 2 });
const refusal = (status: number, code: string, detail = "refused") => new ApiError(status, detail, { code, detail });

/** What the server lists now: every read of the list answers this. */
let listed: unknown[];
beforeEach(() => {
  resetRemoteSharesForTest();
  Object.values(api).forEach((f) => f.mockReset());
  listed = [row(), second];
  api.federationRemote.mockImplementation(async () => ({ remote: listed }));
  api.federationGrantsToPeer.mockResolvedValue({ grants: [] });
  api.federationRemoteFrame.mockImplementation(
    (_p: string, _g: string, _h: string | null, _w: number, signal?: AbortSignal) =>
      new Promise((_resolve, reject) => signal?.addEventListener("abort", () => reject(new Error("aborted")))),
  );
});
afterEach(() => {
  vi.restoreAllMocks();
});

/** Settings → Peers, as the page mounts it: the list comes from the same store. */
function Peers() {
  const remote = useRemoteShares();
  return <PeerShares peerId={GPU} peerName="gpu-box" remote={remote} />;
}
async function openPeers(strict = true) {
  const tree = <Peers />;
  render(strict ? <StrictMode>{tree}</StrictMode> : tree);
  await userEvent.click(await screen.findByRole("button", { name: /Sessions/ }));
  await waitFor(() => expect(screen.getAllByTestId("peer-received")).toHaveLength(2));
}
const leaveButtons = () => screen.getAllByTestId("share-leave");
const dialog = () => screen.getByTestId("leave-dialog");
const leaves = () => api.federationRemoteLeave.mock.calls;

test("every share a peer makes has Leave; pressing it asks first, with focus on Cancel", async () => {
  await openPeers();
  expect(leaveButtons()).toHaveLength(2);
  expect(leaveButtons()[0]).toHaveAccessibleName("Leave Migrate the billing tables, shared by gpu-box");
  await userEvent.click(leaveButtons()[0]);
  expect(dialog()).toHaveAttribute("data-phase", "confirm");
  expect(screen.getByTestId("leave-lead")).toHaveTextContent("Stop receiving Migrate the billing tables from gpu-box?");
  expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  expect(leaves()).toHaveLength(0);
  // Cancel leaves everything as it was, and the button has the focus back
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByTestId("leave-dialog")).toBeNull();
  expect(leaves()).toHaveLength(0);
  expect(screen.getAllByTestId("peer-received")).toHaveLength(2);
  expect(leaveButtons()[0]).toHaveFocus();
});

test("ok: the row is gone at once and stays gone, with the revision that was listed", async () => {
  await openPeers();
  api.federationRemoteLeave.mockImplementation(async () => {
    listed = [second];
    return { left: true };
  });
  await userEvent.click(leaveButtons()[0]);
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(screen.queryByTestId("leave-dialog")).toBeNull());
  expect(leaves()).toEqual([[GPU, GRANT, 4]]);
  expect(screen.getAllByTestId("peer-received")).toHaveLength(1);
  expect(screen.getByTestId("peer-shares-in")).not.toHaveTextContent("Migrate the billing tables");
});

test("ok: a list read cut before the owner ended it cannot bring the row back", async () => {
  await openPeers(false);
  // A poll is in flight when Leave is pressed; it was answered from the list before the leave.
  let late!: (v: unknown) => void;
  api.federationRemote.mockImplementationOnce(() => new Promise((resolve) => (late = resolve)));
  let poll!: Promise<boolean>;
  act(() => {
    poll = refreshRemoteShares();
  });
  api.federationRemoteLeave.mockImplementation(async () => {
    listed = [second];
    return { left: true };
  });
  await userEvent.click(leaveButtons()[0]);
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(screen.getAllByTestId("peer-received")).toHaveLength(1));
  await act(async () => {
    late({ remote: [row(), second] });
    await poll;
  });
  expect(screen.getAllByTestId("peer-received")).toHaveLength(1);
  expect(screen.getByTestId("peer-shares-in")).not.toHaveTextContent("Migrate the billing tables");
});

test("a double press sends one request", async () => {
  await openPeers(false);
  let answer!: (v: unknown) => void;
  api.federationRemoteLeave.mockImplementation(() => new Promise((resolve) => (answer = resolve)));
  await userEvent.click(leaveButtons()[0]);
  const confirm = screen.getByTestId("leave-confirm");
  // two clicks in one task: the second lands before React has rendered the busy dialog
  act(() => {
    confirm.click();
    confirm.click();
  });
  await waitFor(() => expect(confirm).toBeDisabled());
  confirm.click();
  expect(leaves()).toHaveLength(1);
  // while it is in flight nothing dismisses the dialog
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  await userEvent.keyboard("{Escape}");
  expect(dialog()).toBeInTheDocument();
  await act(async () => answer({ left: true }));
  await waitFor(() => expect(screen.queryByTestId("leave-dialog")).toBeNull());
  expect(leaves()).toHaveLength(1);
});

test("offline: nothing changed, the row stays, and Leave cannot be pressed again here", async () => {
  await openPeers();
  api.federationRemoteLeave.mockRejectedValue(refusal(503, "peer_offline"));
  await userEvent.click(leaveButtons()[0]);
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(dialog()).toHaveAttribute("data-phase", "offline"));
  expect(screen.getByTestId("leave-lead")).toHaveTextContent("gpu-box is not connected.");
  expect(screen.getByTestId("leave-sub")).toHaveTextContent("Nothing changed.");
  expect(screen.getByTestId("leave-confirm")).toBeDisabled();
  expect(leaves()).toHaveLength(1);
  await userEvent.click(screen.getByText("Close", { selector: "button" }));
  expect(screen.getAllByTestId("peer-received")).toHaveLength(2);
  // a reopened dialog is the plain question again
  await userEvent.click(leaveButtons()[0]);
  expect(dialog()).toHaveAttribute("data-phase", "confirm");
  expect(screen.getByTestId("leave-confirm")).toBeEnabled();
});

test("refused: the list is read again, the row stays, and Leave sends the revision listed NOW", async () => {
  await openPeers();
  const reads = api.federationRemote.mock.calls.length;
  api.federationRemoteLeave.mockImplementationOnce(async () => {
    listed = [row({ revision: 5, level: "suggest" }), second]; // the owner had just changed it
    throw refusal(404, "no_grant");
  });
  await userEvent.click(leaveButtons()[0]);
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(dialog()).toHaveAttribute("data-phase", "refused"));
  expect(api.federationRemote.mock.calls.length).toBeGreaterThan(reads); // the LIST was read
  expect(leaves()).toEqual([[GPU, GRANT, 4]]); // and the write was not sent again
  expect(screen.getByTestId("leave-lead")).toHaveTextContent("gpu-box did not accept that.");
  expect(screen.getByTestId("leave-sub")).toHaveTextContent("it is still in your list");
  expect(screen.getAllByTestId("peer-received")).toHaveLength(2);
  // Leave is available again, and it is the operator's press that sends it
  api.federationRemoteLeave.mockImplementationOnce(async () => {
    listed = [second];
    return { left: true };
  });
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(screen.queryByTestId("leave-dialog")).toBeNull());
  expect(leaves()).toEqual([
    [GPU, GRANT, 4],
    [GPU, GRANT, 5],
  ]);
  expect(screen.getAllByTestId("peer-received")).toHaveLength(1);
});

test("refused, and the list no longer has it: the dialog closes with the row", async () => {
  await openPeers();
  api.federationRemoteLeave.mockImplementationOnce(async () => {
    listed = [second]; // the owner had revoked it
    throw refusal(404, "no_grant");
  });
  await userEvent.click(leaveButtons()[0]);
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(screen.queryByTestId("leave-dialog")).toBeNull());
  expect(screen.getAllByTestId("peer-received")).toHaveLength(1);
  expect(leaves()).toHaveLength(1);
});

test.each([
  ["504 peer_lost", refusal(504, "peer_lost")],
  ["504 timeout", refusal(504, "timeout")],
  ["429 busy", refusal(429, "busy")],
  ["the network", new TypeError("Failed to fetch")],
  ["an unknown code", refusal(503, "something_new")],
  ["a 500", refusal(500, "store")],
])("uncertain (%s): the row stays, and Check again READS the list and never sends Leave", async (_name, failure) => {
  await openPeers();
  api.federationRemoteLeave.mockRejectedValue(failure);
  await userEvent.click(leaveButtons()[0]);
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(dialog()).toHaveAttribute("data-phase", "unconfirmed"));
  expect(screen.getByTestId("leave-lead")).toHaveTextContent("We could not confirm that you left.");
  expect(dialog()).not.toHaveTextContent(/did not accept|is not connected/); // never a refusal
  expect(screen.getAllByTestId("peer-received")).toHaveLength(2); // never shown as left
  expect(screen.queryByTestId("leave-confirm")).toBeNull(); // no Leave to press here
  expect(leaves()).toHaveLength(1);

  // Check again: the list is read; it still has the share
  let reads = api.federationRemote.mock.calls.length;
  await userEvent.click(screen.getByTestId("leave-check"));
  await waitFor(() => expect(screen.getByTestId("leave-note")).toHaveTextContent("still in your list"));
  expect(api.federationRemote.mock.calls.length).toBe(reads + 1);
  expect(leaves()).toHaveLength(1);

  // Check again while the list cannot be read: said as that, and nothing is concluded
  api.federationRemote.mockRejectedValueOnce(new Error("offline"));
  await userEvent.click(screen.getByTestId("leave-check"));
  await waitFor(() => expect(screen.getByTestId("leave-note")).toHaveTextContent("could not be read"));
  expect(screen.getAllByTestId("peer-received")).toHaveLength(2);
  expect(leaves()).toHaveLength(1);

  // Check again once the owner's list no longer has it: the dialog closes with the row
  listed = [second];
  reads = api.federationRemote.mock.calls.length;
  await userEvent.click(screen.getByTestId("leave-check"));
  await waitFor(() => expect(screen.queryByTestId("leave-dialog")).toBeNull());
  expect(screen.getAllByTestId("peer-received")).toHaveLength(1);
  expect(api.federationRemote.mock.calls.length).toBe(reads + 1);
  expect(leaves()).toHaveLength(1); // one Leave, in all of this
});

test("another proven refusal says the server's sentence and keeps the row", async () => {
  await openPeers();
  api.federationRemoteLeave.mockRejectedValue(
    refusal(422, "not_possible", "that isn't possible for this kind of share"),
  );
  await userEvent.click(leaveButtons()[0]);
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(dialog()).toHaveAttribute("data-phase", "rejected"));
  expect(screen.getByTestId("leave-sub")).toHaveTextContent(
    "That isn't possible for this kind of share. Nothing changed: the share is still in your list.",
  );
  expect(screen.getAllByTestId("peer-received")).toHaveLength(2);
});

test("a state is for one revision: when the owner changes the share it is the plain question again", async () => {
  await openPeers();
  api.federationRemoteLeave.mockRejectedValue(refusal(503, "peer_offline"));
  await userEvent.click(leaveButtons()[0]);
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(dialog()).toHaveAttribute("data-phase", "offline"));
  expect(screen.getByTestId("leave-confirm")).toBeDisabled();
  // a list read brings a new revision of the same share
  listed = [row({ revision: 5 }), second];
  await act(async () => {
    await refreshRemoteShares();
  });
  expect(dialog()).toHaveAttribute("data-phase", "confirm");
  expect(screen.getByTestId("leave-confirm")).toBeEnabled();
  api.federationRemoteLeave.mockResolvedValue({ left: true });
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(leaves()).toHaveLength(2));
  expect(leaves()[1]).toEqual([GPU, GRANT, 5]); // the revision listed at the press
});

test("a state is for one share: another share's dialog starts clean", async () => {
  await openPeers();
  api.federationRemoteLeave.mockRejectedValue(refusal(504, "peer_lost"));
  await userEvent.click(leaveButtons()[0]);
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(dialog()).toHaveAttribute("data-phase", "unconfirmed"));
  await userEvent.click(screen.getByText("Close", { selector: "button" }));
  await userEvent.click(leaveButtons()[1]);
  expect(dialog()).toHaveAttribute("data-phase", "confirm");
  expect(screen.getByTestId("leave-lead")).toHaveTextContent("Stop receiving Bench the new tokenizer from gpu-box?");
  api.federationRemoteLeave.mockResolvedValue({ left: true });
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(leaves()).toHaveLength(2));
  expect(leaves()[1]).toEqual([GPU, OTHER, 2]);
});

test("a late uncertain answer cannot undo what a newer list read settled", async () => {
  await openPeers(false);
  let fail!: (e: unknown) => void;
  api.federationRemoteLeave.mockImplementation(() => new Promise((_resolve, reject) => (fail = reject)));
  await userEvent.click(leaveButtons()[0]);
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(leaves()).toHaveLength(1));
  // while Leave is in flight, a newer list read says the share is gone (the owner ended it)
  listed = [second];
  await act(async () => {
    await refreshRemoteShares();
  });
  expect(screen.getAllByTestId("peer-received")).toHaveLength(1);
  // the Leave's own answer arrives late, and uncertain: it puts nothing back and claims nothing
  await act(async () => fail(refusal(504, "peer_lost")));
  await waitFor(() => expect(screen.queryByTestId("leave-dialog")).toBeNull());
  expect(screen.getAllByTestId("peer-received")).toHaveLength(1);
  expect(screen.getByTestId("peer-shares-in")).not.toHaveTextContent("Migrate the billing tables");
  expect(leaves()).toHaveLength(1);
});

test("a late refusal is bound to the revision listed when it arrives, not the one it was sent under", async () => {
  await openPeers(false);
  let fail!: (e: unknown) => void;
  api.federationRemoteLeave.mockImplementationOnce(() => new Promise((_resolve, reject) => (fail = reject)));
  await userEvent.click(leaveButtons()[0]);
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(leaves()).toHaveLength(1));
  listed = [row({ revision: 6 }), second];
  await act(async () => {
    await refreshRemoteShares();
  });
  // in flight: still busy, whatever the list says (no second request can start)
  expect(screen.getByTestId("leave-confirm")).toBeDisabled();
  await act(async () => fail(refusal(404, "no_grant")));
  await waitFor(() => expect(dialog()).toHaveAttribute("data-phase", "refused"));
  expect(screen.getAllByTestId("peer-received")).toHaveLength(2); // the row a read listed stays
  api.federationRemoteLeave.mockResolvedValue({ left: true });
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(leaves()).toHaveLength(2));
  expect(leaves()[1]).toEqual([GPU, GRANT, 6]);
});

// ---- the shared session's own view ---------------------------------------------------------------

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}
function openScreen() {
  listed = [row({ kind: "terminal", title: "Bench the new tokenizer" })];
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={[remotePath(GPU, GRANT)]}>
        <Routes>
          <Route path="/remote/:peerId/:grantId" element={<RemoteScreenPane peerId={GPU} grantId={GRANT} />} />
          <Route path="*" element={<p>the session list</p>} />
        </Routes>
        <Where />
      </MemoryRouter>
    </StrictMode>,
  );
}

test("in the session's header: a plain button in the banner, and a proven leave goes to the session list", async () => {
  openScreen();
  const button = await screen.findByTestId("share-leave");
  expect(screen.getByTestId("remote-banner")).toContainElement(button);
  expect(button.closest('[role="menu"]')).toBeNull();
  api.federationRemoteLeave.mockImplementation(async () => {
    listed = [];
    return { left: true };
  });
  await userEvent.click(button);
  expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  expect(screen.getByTestId("where")).toHaveTextContent(remotePath(GPU, GRANT));
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(SESSIONS_PATH));
  expect(leaves()).toEqual([[GPU, GRANT, 4]]);
  expect(screen.queryByTestId("leave-dialog")).toBeNull();
});

test("in the session's header: an uncertain answer stays on the session until a read shows it gone", async () => {
  openScreen();
  api.federationRemoteLeave.mockRejectedValue(refusal(504, "peer_lost"));
  await userEvent.click(await screen.findByTestId("share-leave"));
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(dialog()).toHaveAttribute("data-phase", "unconfirmed"));
  expect(screen.getByTestId("where")).toHaveTextContent(remotePath(GPU, GRANT)); // not shown as left
  await userEvent.click(screen.getByTestId("leave-check"));
  await waitFor(() => expect(screen.getByTestId("leave-note")).toBeInTheDocument());
  expect(screen.getByTestId("where")).toHaveTextContent(remotePath(GPU, GRANT));
  listed = [];
  await userEvent.click(screen.getByTestId("leave-check"));
  await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(SESSIONS_PATH));
  expect(leaves()).toHaveLength(1);
});

test("in the session's header: Cancel, and an offline peer, leave the session where it is", async () => {
  openScreen();
  await userEvent.click(await screen.findByTestId("share-leave"));
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(leaves()).toHaveLength(0);
  api.federationRemoteLeave.mockRejectedValue(refusal(503, "peer_offline"));
  await userEvent.click(screen.getByTestId("share-leave"));
  await userEvent.click(screen.getByTestId("leave-confirm"));
  await waitFor(() => expect(dialog()).toHaveAttribute("data-phase", "offline"));
  await userEvent.click(screen.getByText("Close", { selector: "button" }));
  expect(screen.getByTestId("where")).toHaveTextContent(remotePath(GPU, GRANT));
  expect(screen.getByTestId("share-leave")).toBeInTheDocument();
});
