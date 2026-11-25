import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { PEER_DISCLOSURE, PEER_DISCLOSURE_DETAIL } from "../../lib/federation";

// Share with a peer… (#2055): what the dialog asks the server for, and what it shows of the
// answers. Layout, the phone sheet and focus are the browser spec's (e2e/share-peer.spec.ts).
const api = vi.hoisted(() => ({
  federation: vi.fn(),
  federationGrants: vi.fn(),
  createFederationGrant: vi.fn(),
  changeFederationGrant: vi.fn(),
  revokeFederationGrant: vi.fn(),
  federationAccessRequests: vi.fn(),
  declineFederationAccess: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  api,
}));

const { ApiError } = await import("../../lib/api");
const { SharePeerModal } = await import("./SharePeerModal");

const KEY = "codex-api:0d0f6a52-1111-4222-8333-444455556666";
const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const LAPTOP = "b7nd4qzse2vkx6mhp3ytw5caj4ruo2gf";
const OLD = "t2hy6wfnc5qdk7ezr3mbx4sav6pju2lo";
const NOW = Date.now() / 1000;

const peer = (peer_id: string, name: string, over = {}) => ({
  peer_id,
  name,
  state: "trusted",
  connected: true,
  refused: false,
  reachable_by: ["direct"],
  created_at: NOW - 86400,
  last_seen_at: NOW - 5,
  revision: 1,
  ...over,
});
const state = (peers: unknown[]) => ({
  owner: true,
  self: { peer_id: "k3wq7n2bxd5mr4tzh6pac2vyj7uef3so", name: "studio", reachable_by: ["direct"] },
  peers,
  invites: [],
  pairings: [],
});
const grant = (over = {}) => ({
  grant_id: "g7Jx9aBcDeFgHiJkLmNoPq",
  session_key: KEY,
  peer_id: GPU,
  peer_name: "gpu-box",
  level: "view",
  expires_at: NOW + 3 * 86400,
  revision: 4,
  state: "live",
  created_at: NOW - 86400,
  ended_at: null,
  end_reason: null,
  ...over,
});
const list = (grants: unknown[]) => ({
  grants,
  generation: 1,
  levels: ["view"],
  default_expires_in: { view: 604800 },
  max_expires_in: 7776000,
});

function open(onClose = vi.fn(), missionHeld = false) {
  return {
    onClose,
    ...render(
      <MemoryRouter>
        <SharePeerModal
          sessionKey={KEY}
          title="Refactor the importer"
          agent="API"
          missionHeld={missionHeld}
          onClose={onClose}
        />
      </MemoryRouter>,
    ),
  };
}
const level = (name: string) =>
  within(screen.getByRole("radiogroup", { name: "Access" })).getByRole("radio", { name: new RegExp(`^${name}`) });
const share = () => screen.getByRole("button", { name: "Share" });
const ack = () => screen.getByRole("checkbox", { name: "I understand and want to allow this." });

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
  api.federation.mockResolvedValue(
    state([
      peer(GPU, "gpu-box"),
      peer(LAPTOP, "laptop", { connected: false, last_seen_at: NOW - 7200 }),
      peer(OLD, "old-desktop", { state: "revoked" }),
    ]),
  );
  api.federationGrants.mockResolvedValue(list([]));
});

test("it lists trusted peers with their state, View chosen, 7 days, and the disclosure", async () => {
  open();
  const peers = await screen.findByRole("radiogroup", { name: "Peer" });
  const tiles = within(peers).getAllByRole("radio");
  expect(tiles.map((t) => t.textContent)).toEqual([
    "gpu-boxconnected",
    "laptopoffline · last seen 2 h ago",
  ]); // the revoked peer is not offered
  expect(tiles[0]).toHaveAttribute("aria-checked", "true");

  const access = within(screen.getByRole("radiogroup", { name: "Access" })).getAllByRole("radio");
  expect(access[0]).toHaveAttribute("aria-checked", "true");
  expect(access[0]).toHaveTextContent("View");
  expect(access[1]).toBeEnabled();
  expect(access[1]).toHaveTextContent("SuggestProposes messages. You approve each one.");
  expect(access[1]).toHaveAttribute("aria-checked", "false");
  expect(access[2]).toBeEnabled();
  expect(access[2]).toHaveTextContent("SendMessages go straight to the agent.");
  expect(access[2]).toHaveAttribute("aria-checked", "false"); // never preselected

  const expiry = within(screen.getByRole("radiogroup", { name: "Expires" })).getAllByRole("radio");
  expect(expiry.map((b) => b.textContent)).toEqual(["1 day", "7 days", "30 days", "90 days"]);
  expect(expiry[1]).toHaveAttribute("aria-checked", "true");

  expect(screen.getByTestId("share-peer-disclosure")).toHaveTextContent(
    `${PEER_DISCLOSURE}${PEER_DISCLOSURE_DETAIL}`,
  );
});

test("Share creates a view grant for the chosen peer and expiry, and lists it", async () => {
  api.createFederationGrant.mockResolvedValue({ grant: grant({ peer_id: LAPTOP }), generation: 2 });
  open();
  await userEvent.click(await screen.findByRole("radio", { name: /laptop/ }));
  // An offline peer can be granted access; the dialog says what that means.
  expect(screen.getByTestId("share-peer-offline")).toHaveTextContent(
    "laptop is offline. It sees the session when it next connects.",
  );
  await userEvent.click(screen.getByRole("radio", { name: "30 days" }));
  api.federationGrants.mockResolvedValue(
    list([grant({ peer_id: LAPTOP, peer_name: "laptop" })]),
  );
  await userEvent.click(screen.getByRole("button", { name: "Share" }));
  await waitFor(() =>
    expect(api.createFederationGrant).toHaveBeenCalledWith({
      session_key: KEY,
      peer_id: LAPTOP,
      level: "view",
      expires_in: 30 * 86400,
    }),
  );
  expect(api.changeFederationGrant).not.toHaveBeenCalled();
  const row = await screen.findByTestId("share-peer-grant");
  expect(row).toHaveTextContent(/laptop\s*View\s*until/);
  expect(screen.getByRole("heading", { name: "Shared with · 1" })).toBeInTheDocument();
  expect(screen.getByTestId("share-peer-done")).toHaveTextContent(/^Shared with laptop \(View\) until /);
});

test("sharing again with the same peer changes the existing grant with its revision", async () => {
  api.federationGrants.mockResolvedValue(list([grant()]));
  api.changeFederationGrant.mockResolvedValue({ grant: grant({ revision: 5 }), generation: 3 });
  open();
  expect(await screen.findByTestId("share-peer-existing")).toHaveTextContent(
    /Already shared with gpu-box \(View\), until .*Sharing again replaces its access and when it ends\./,
  );
  await userEvent.click(screen.getByRole("radio", { name: "90 days" }));
  await userEvent.click(screen.getByRole("button", { name: "Share" }));
  await waitFor(() =>
    expect(api.changeFederationGrant).toHaveBeenCalledWith("g7Jx9aBcDeFgHiJkLmNoPq", {
      level: "view",
      expires_in: 90 * 86400,
      revision: 4,
    }),
  );
  expect(api.createFederationGrant).not.toHaveBeenCalled();
});

test("Revoke ends that grant and the list is read again", async () => {
  api.federationGrants.mockResolvedValue(list([grant()]));
  api.revokeFederationGrant.mockResolvedValue({ grant: grant({ state: "revoked" }), generation: 4 });
  open();
  const revoke = await screen.findByRole("button", { name: "Revoke gpu-box" });
  api.federationGrants.mockResolvedValue(list([]));
  await userEvent.click(revoke);
  await waitFor(() =>
    expect(api.revokeFederationGrant).toHaveBeenCalledWith("g7Jx9aBcDeFgHiJkLmNoPq"),
  );
  await waitFor(() => expect(screen.queryByTestId("share-peer-grant")).toBeNull());
});

test("a refusal is shown with the server's own words, and the dialog stays open", async () => {
  api.createFederationGrant.mockRejectedValue(
    new ApiError(409, "this peer already holds 200 shared sessions"),
  );
  const { onClose } = open();
  await userEvent.click(await screen.findByRole("button", { name: "Share" }));
  expect(await screen.findByTestId("share-peer-error")).toHaveTextContent(
    "this peer already holds 200 shared sessions",
  );
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Share" })).toBeEnabled();
});

test("with no paired instances it says so, links to Settings → Peers, and cannot share", async () => {
  api.federation.mockResolvedValue(state([peer(OLD, "old-desktop", { state: "revoked" })]));
  open();
  const none = await screen.findByTestId("share-peer-none");
  expect(none).toHaveTextContent("No paired instances yet.");
  expect(within(none).getByRole("link", { name: "Open Settings → Peers" })).toHaveAttribute(
    "href",
    "/settings/peers",
  );
  expect(screen.getByRole("button", { name: "Share" })).toBeDisabled();
  expect(screen.queryByRole("radiogroup", { name: "Peer" })).toBeNull();
});

test("Escape and Cancel close it", async () => {
  const { onClose } = open();
  await screen.findByRole("radiogroup", { name: "Peer" });
  await userEvent.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalledTimes(1);
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onClose).toHaveBeenCalledTimes(2);
});

// ---- the levels (#2061) -----------------------------------------------------------------------
const expiries = () =>
  within(screen.getByRole("radiogroup", { name: "Expires" }))
    .getAllByRole("radio")
    .map((b) => `${b.textContent}${b.getAttribute("aria-checked") === "true" ? "*" : ""}`);

test("Suggest: 1, 7 (default), 30 days, its line, and a request without accept_risk", async () => {
  api.createFederationGrant.mockResolvedValue({ grant: grant({ level: "suggest" }), generation: 2 });
  open();
  await screen.findByRole("radiogroup", { name: "Peer" });
  await userEvent.click(level("Suggest"));
  expect(expiries()).toEqual(["1 day", "7 days*", "30 days"]);
  expect(screen.getByTestId("share-peer-suggest-line")).toHaveTextContent(
    "A suggestion reaches the agent only after you approve that one message. Nothing approves it for you.",
  );
  expect(screen.queryByTestId("share-peer-risk")).toBeNull();
  await userEvent.click(share());
  await waitFor(() =>
    expect(api.createFederationGrant).toHaveBeenCalledWith({
      session_key: KEY,
      peer_id: GPU,
      level: "suggest",
      expires_in: 7 * 86400,
    }),
  );
});

test("Send: 1 hour (default), 4, 24 hours; the risk box; Share waits for the tick", async () => {
  api.createFederationGrant.mockResolvedValue({
    grant: grant({ level: "send", expires_at: NOW + 4 * 3600 }),
    generation: 2,
  });
  open();
  await screen.findByRole("radiogroup", { name: "Peer" });
  await userEvent.click(level("Send"));
  expect(expiries()).toEqual(["1 hour*", "4 hours", "24 hours"]);
  expect(screen.getByTestId("share-peer-hint")).toHaveTextContent(
    "It ends by itself after 1 hour. Only you can renew it.",
  );
  const risk = screen.getByTestId("share-peer-risk");
  expect(risk).toHaveTextContent(
    "RiskSend lets gpu-box's operator instruct this agent directly. Whatever this agent can do on this machine, their messages can ask it to do.",
  );
  expect(ack()).not.toBeChecked();
  expect(ack()).toBeRequired();
  expect(share()).toBeDisabled();
  await userEvent.click(share()); // a disabled button does nothing
  expect(api.createFederationGrant).not.toHaveBeenCalled();

  await userEvent.click(screen.getByRole("radio", { name: "4 hours" }));
  expect(screen.getByTestId("share-peer-hint")).toHaveTextContent("after 4 hours");
  expect(share()).toBeDisabled(); // an expiry is not an acknowledgement
  await userEvent.click(ack());
  expect(share()).toBeEnabled();
  await userEvent.click(share());
  await waitFor(() =>
    expect(api.createFederationGrant).toHaveBeenCalledWith({
      session_key: KEY,
      peer_id: GPU,
      level: "send",
      expires_in: 4 * 3600,
      accept_risk: true,
    }),
  );
  // one acknowledgement, one grant: sharing again asks again
  await waitFor(() => expect(ack()).not.toBeChecked());
  expect(share()).toBeDisabled();
});

test("the acknowledgement unticks on another peer, another level, and a reopened dialog", async () => {
  api.federationGrants.mockResolvedValue(list([grant({ level: "send", expires_at: NOW + 1800 })]));
  const first = open();
  await screen.findByRole("radiogroup", { name: "Peer" });
  // a session that already has a Send grant still opens on View
  expect(level("View")).toHaveAttribute("aria-checked", "true");
  expect(level("Send")).toHaveAttribute("aria-checked", "false");
  expect(share()).toBeEnabled();

  await userEvent.click(level("Send"));
  await userEvent.click(ack());
  expect(share()).toBeEnabled();
  await userEvent.click(screen.getByRole("radio", { name: /laptop/ })); // another peer
  expect(level("Send")).toHaveAttribute("aria-checked", "true");
  expect(ack()).not.toBeChecked();
  expect(share()).toBeDisabled();
  expect(screen.getByTestId("share-peer-risk")).toHaveTextContent("Send lets laptop's operator");

  await userEvent.click(ack());
  await userEvent.click(level("Suggest")); // another level
  expect(share()).toBeEnabled();
  await userEvent.click(level("Send")); // and back
  expect(ack()).not.toBeChecked();
  expect(share()).toBeDisabled();

  await userEvent.click(ack());
  first.unmount(); // closed
  open(); // and opened again
  await screen.findByRole("radiogroup", { name: "Peer" });
  expect(level("View")).toHaveAttribute("aria-checked", "true");
  await userEvent.click(level("Send"));
  expect(ack()).not.toBeChecked();
  expect(share()).toBeDisabled();
  expect(api.createFederationGrant).not.toHaveBeenCalled();
  expect(api.changeFederationGrant).not.toHaveBeenCalled();
});

test("renewing a Send grant is the same flow: the PATCH carries accept_risk only after the tick", async () => {
  api.federationGrants.mockResolvedValue(list([grant({ level: "send", expires_at: NOW + 600 })]));
  api.changeFederationGrant.mockResolvedValue({ grant: grant({ level: "send", revision: 5 }), generation: 3 });
  open();
  await screen.findByRole("radiogroup", { name: "Peer" });
  await userEvent.click(level("Send"));
  expect(share()).toBeDisabled();
  await userEvent.click(ack());
  await userEvent.click(share());
  await waitFor(() =>
    expect(api.changeFederationGrant).toHaveBeenCalledWith("g7Jx9aBcDeFgHiJkLmNoPq", {
      level: "send",
      expires_in: 3600,
      accept_risk: true,
      revision: 4,
    }),
  );
});

test("existing shares show their level, and a countdown when they end within a day", async () => {
  api.federationGrants.mockResolvedValue(
    list([
      grant({ grant_id: "a".repeat(22), peer_id: LAPTOP, peer_name: "laptop" }),
      grant({ level: "send", expires_at: NOW + 42 * 60 + 20 }),
    ]),
  );
  open();
  const rows = await screen.findAllByTestId("share-peer-grant");
  expect(rows[0]).toHaveTextContent(/laptop\s*View\s*until /);
  expect(rows[1]).toHaveTextContent(/gpu-box\s*Send\s*ends in 42 min/);
});

test("a mission-held session offers only View, with the line", async () => {
  open(vi.fn(), true);
  await screen.findByRole("radiogroup", { name: "Peer" });
  expect(level("View")).toHaveAttribute("aria-checked", "true");
  expect(level("Suggest")).toBeDisabled();
  expect(level("Send")).toBeDisabled();
  expect(screen.getByTestId("share-peer-held")).toHaveTextContent(
    "This session belongs to a mission right now. It can only be watched.",
  );
  expect(screen.queryByTestId("share-peer-risk")).toBeNull();
  expect(share()).toBeEnabled();
});

test("the server's 409 held closes the two higher levels, whatever the list said", async () => {
  api.createFederationGrant.mockRejectedValue(
    new ApiError(409, "this session is part of a mission", { code: "held" }),
  );
  open();
  await screen.findByRole("radiogroup", { name: "Peer" });
  await userEvent.click(level("Send"));
  await userEvent.click(ack());
  await userEvent.click(share());
  expect(await screen.findByTestId("share-peer-held")).toBeInTheDocument();
  expect(level("Send")).toBeDisabled();
  expect(level("View")).toHaveAttribute("aria-checked", "true");
  expect(screen.queryByTestId("share-peer-done")).toBeNull();
});

test("503 busy on Revoke: nothing changed, a retry, never success", async () => {
  api.federationGrants.mockResolvedValue(list([grant({ level: "send", expires_at: NOW + 1800 })]));
  api.revokeFederationGrant.mockRejectedValueOnce(
    new ApiError(503, "a delivery under this grant is in flight — try again", { code: "busy" }),
  );
  open();
  await userEvent.click(await screen.findByRole("button", { name: "Revoke gpu-box" }));
  const stuck = await screen.findByTestId("share-peer-busy");
  expect(stuck).toHaveTextContent("A message is being delivered. Nothing changed. Try again.");
  expect(screen.queryByTestId("share-peer-done")).toBeNull();
  expect(screen.getByTestId("share-peer-grant")).toBeInTheDocument(); // still listed: still live
  api.revokeFederationGrant.mockResolvedValue({ grant: grant({ state: "revoked" }), generation: 4 });
  api.federationGrants.mockResolvedValue(list([]));
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(api.revokeFederationGrant).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByTestId("share-peer-busy")).toBeNull());
  await waitFor(() => expect(screen.queryByTestId("share-peer-grant")).toBeNull());
});

test("503 busy on a change: the same state, and the grant is not reported as shared", async () => {
  api.federationGrants.mockResolvedValue(list([grant()]));
  api.changeFederationGrant.mockRejectedValue(new ApiError(503, "busy", { code: "busy" }));
  open();
  await screen.findByTestId("share-peer-existing");
  await userEvent.click(share());
  expect(await screen.findByTestId("share-peer-busy")).toHaveTextContent(
    "A message is being delivered. Nothing changed. Try again.",
  );
  expect(screen.queryByTestId("share-peer-done")).toBeNull();
  expect(screen.queryByTestId("share-peer-error")).toBeNull();
});

// ---- a terminal (#2060) -------------------------------------------------------------------------

const TERM_KEY = "claude:7c1e3d2f-9a4b-4d8e-8f32-7b5c4d3e2f10";
function openTerminal() {
  render(
    <MemoryRouter>
      <SharePeerModal
        kind="terminal"
        sessionKey={TERM_KEY}
        title="Deploy the staging stack"
        agent="claude"
        onClose={vi.fn()}
      />
    </MemoryRouter>,
  );
  return screen.getByRole("dialog", { name: "Share with a peer" });
}

test("a terminal: the limit is the first thing in the body, with the unprotected case in bold", async () => {
  const dialog = openTerminal();
  await within(dialog).findByRole("radio", { name: /gpu-box/ });
  const body = within(dialog).getByTestId("share-peer-body");
  const limit = within(dialog).getByTestId("share-peer-limit");
  expect(body.firstElementChild).toBe(limit);
  // What is masked, what is not, and what is never sent: docs/invariants/federation.md's stated
  // limits and the renderer's one-cell allowlist, in the dialog's words.
  expect(limit.textContent).toBe(
    "Know the limit" +
      "The peer sees this terminal's screen. A secret we know of is hidden when all of it is on one line or it wraps at the screen's edge. " +
      "A secret that is cut off, one a program lays out across lines itself, or one we don't know about is not hidden." +
      "No screen is sent after a control we can't redraw exactly, such as an edit in the middle of a shell line, or while the screen shows any character outside plain Latin, Greek and Cyrillic text, punctuation, arrows, symbols and box drawing: emoji, CJK, Hangul, Arabic, Indic and similar scripts, or combining characters. An agent that draws emoji is often not viewable." +
      "It is a picture of the screen as text. The peer cannot type, resize or scroll back.",
  );
  expect(limit.querySelector("b")?.textContent).toBe(
    "A secret that is cut off, one a program lays out across lines itself, or one we don't know about is not hidden.",
  );
  expect(within(limit).getByTestId("share-peer-not-shown").textContent).toBe(
    "No screen is sent after a control we can't redraw exactly, such as an edit in the middle of a shell line, or while the screen shows any character outside plain Latin, Greek and Cyrillic text, punctuation, arrows, symbols and box drawing: emoji, CJK, Hangul, Arabic, Indic and similar scripts, or combining characters. An agent that draws emoji is often not viewable.",
  );
  // Before the peer picker in the document, not only in the box model.
  const firstPeer = within(dialog).getByRole("radio", { name: /gpu-box/ });
  expect(limit.compareDocumentPosition(firstPeer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  // The session line says what it is, and the API disclosure is not this dialog's.
  expect(dialog).toHaveTextContent("Terminal");
  expect(within(dialog).queryByTestId("share-peer-disclosure")).toBeNull();
});

test("a terminal: the limit is there before anything loaded, and with no paired instance", async () => {
  api.federation.mockResolvedValue(state([]));
  const dialog = openTerminal();
  expect(within(dialog).getByTestId("share-peer-limit")).toBeInTheDocument(); // not waited for
  await within(dialog).findByTestId("share-peer-none");
  expect(within(dialog).getByTestId("share-peer-limit")).toBeInTheDocument();
});

test("a terminal: Access offers View alone, and says a terminal can only be watched", async () => {
  const dialog = openTerminal();
  await within(dialog).findByRole("radio", { name: /gpu-box/ });
  const access = within(dialog).getByRole("radiogroup", { name: "Access" });
  const levels = within(access).getAllByRole("radio");
  expect(levels).toHaveLength(1);
  expect(levels[0]).toHaveTextContent("View");
  expect(levels[0]).toHaveTextContent("Watch the screen");
  expect(levels[0]).toHaveAttribute("aria-checked", "true");
  expect(within(dialog).queryByText("Suggest")).toBeNull();
  expect(within(dialog).queryByText("Send")).toBeNull();
  expect(within(dialog).queryByText("later")).toBeNull();
  expect(within(dialog).getByTestId("share-peer-terminal-only")).toHaveTextContent(
    "A terminal can only be watched.",
  );
});

test("a terminal: the grant is made with the terminal session's key, and its shares are revocable", async () => {
  api.federationGrants.mockResolvedValue(
    list([grant({ session_key: TERM_KEY, kind: "terminal", peer_id: LAPTOP, peer_name: "laptop" })]),
  );
  api.createFederationGrant.mockResolvedValue({
    grant: grant({ session_key: TERM_KEY, kind: "terminal", expires_at: NOW + 7 * 86400 }),
    generation: 2,
  });
  const dialog = openTerminal();
  await within(dialog).findByRole("radio", { name: /gpu-box/ });
  expect(api.federationGrants).toHaveBeenCalledWith(TERM_KEY);
  await userEvent.click(within(dialog).getByTestId("share-peer-go"));
  await waitFor(() =>
    expect(api.createFederationGrant).toHaveBeenCalledWith({
      session_key: TERM_KEY,
      peer_id: GPU,
      level: "view",
      expires_in: 7 * 86400,
    }),
  );
  await within(dialog).findByTestId("share-peer-done");
  api.revokeFederationGrant.mockResolvedValue({ grant: grant(), generation: 3 });
  await userEvent.click(within(dialog).getByRole("button", { name: "Revoke laptop" }));
  await waitFor(() => expect(api.revokeFederationGrant).toHaveBeenCalledWith("g7Jx9aBcDeFgHiJkLmNoPq"));
});

// #2055 review (P2) for the terminal variant: under <StrictMode> (main.tsx) the dialog must
// finish loading, show the session's existing terminal shares, and take a share's answer.
test("a terminal under StrictMode: the limit, the peers, the shares, and a share's answer", async () => {
  api.federationGrants.mockResolvedValue(
    list([grant({ session_key: TERM_KEY, kind: "terminal", peer_id: LAPTOP, peer_name: "laptop" })]),
  );
  api.createFederationGrant.mockResolvedValue({
    grant: grant({ session_key: TERM_KEY, kind: "terminal", expires_at: NOW + 7 * 86400 }),
    generation: 2,
  });
  render(
    <StrictMode>
      <MemoryRouter>
        <SharePeerModal
          kind="terminal"
          sessionKey={TERM_KEY}
          title="Deploy the staging stack"
          agent="claude"
          onClose={vi.fn()}
        />
      </MemoryRouter>
    </StrictMode>,
  );
  const dialog = screen.getByRole("dialog", { name: "Share with a peer" });
  expect(within(dialog).getByTestId("share-peer-limit")).toBeInTheDocument();
  expect(await within(dialog).findByRole("radiogroup", { name: "Peer" })).toBeInTheDocument();
  expect(await within(dialog).findByTestId("share-peer-grant")).toHaveTextContent(/laptop\s*View\s*until/);
  expect(within(dialog).queryByText("Loading the paired instances…")).toBeNull();
  const go = within(dialog).getByTestId("share-peer-go");
  await waitFor(() => expect(go).toBeEnabled());
  await userEvent.click(go);
  expect(await within(dialog).findByTestId("share-peer-done")).toHaveTextContent(/^Shared with gpu-box \(View\) until /);
  await waitFor(() => expect(go).toBeEnabled());
});

test("a terminal: a refusal is shown with the server's words", async () => {
  api.createFederationGrant.mockRejectedValue(new ApiError(422, "a terminal session can only be viewed"));
  const dialog = openTerminal();
  await within(dialog).findByRole("radio", { name: /gpu-box/ });
  await userEvent.click(within(dialog).getByTestId("share-peer-go"));
  expect(await within(dialog).findByTestId("share-peer-error")).toHaveTextContent(
    "a terminal session can only be viewed",
  );
});

// #2055 review (P2): the app mounts under <StrictMode> (main.tsx). Its effect replay ran the old
// `alive` ref's cleanup and nothing set it again, so every answer was discarded: the dialog said
// "Loading the paired instances…" for ever and its "Shared with" list stayed empty.
test("under StrictMode the dialog finishes loading: peers, and the session's shares", async () => {
  api.federationGrants.mockResolvedValue(list([grant()]));
  render(
    <StrictMode>
      <MemoryRouter>
        <SharePeerModal sessionKey={KEY} title="Refactor the importer" agent="API" onClose={vi.fn()} />
      </MemoryRouter>
    </StrictMode>,
  );
  expect(await screen.findByRole("radiogroup", { name: "Peer" })).toBeInTheDocument();
  expect(await screen.findByTestId("share-peer-grant")).toHaveTextContent(/gpu-box\s*View\s*until/);
  expect(screen.queryByText("Loading the paired instances…")).toBeNull();
  expect(screen.getByRole("button", { name: "Share" })).toBeEnabled();
});

test("under StrictMode a share's answer and the list read after it are shown", async () => {
  api.createFederationGrant.mockResolvedValue({ grant: grant(), generation: 2 });
  render(
    <StrictMode>
      <MemoryRouter>
        <SharePeerModal sessionKey={KEY} title="Refactor the importer" agent="API" onClose={vi.fn()} />
      </MemoryRouter>
    </StrictMode>,
  );
  const go = await screen.findByRole("button", { name: "Share" });
  await waitFor(() => expect(go).toBeEnabled());
  api.federationGrants.mockResolvedValue(list([grant()]));
  await userEvent.click(go);
  expect(await screen.findByTestId("share-peer-done")).toHaveTextContent(/^Shared with gpu-box \(View\) until /);
  expect(await screen.findByTestId("share-peer-grant")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("button", { name: "Share" })).toBeEnabled());
});

test("a late answer for the session the dialog showed before is dropped", async () => {
  // The fence that replaced `alive` must still keep stale answers out: the first session's list
  // arrives after the dialog moved to another session, and is never shown there.
  let late: (v: unknown) => void = () => {};
  api.federationGrants.mockImplementation((key: string) =>
    key === KEY
      ? new Promise((resolve) => (late = resolve))
      : Promise.resolve(list([])),
  );
  const other = "codex-api:0d0f6a52-9999-4222-8333-444455556666";
  const { rerender } = render(
    <StrictMode>
      <MemoryRouter>
        <SharePeerModal sessionKey={KEY} title="Refactor the importer" agent="API" onClose={vi.fn()} />
      </MemoryRouter>
    </StrictMode>,
  );
  await screen.findByRole("radiogroup", { name: "Peer" });
  rerender(
    <StrictMode>
      <MemoryRouter>
        <SharePeerModal sessionKey={other} title="Another one" agent="API" onClose={vi.fn()} />
      </MemoryRouter>
    </StrictMode>,
  );
  await waitFor(() => expect(api.federationGrants).toHaveBeenCalledWith(other));
  await waitFor(() => expect(screen.getByRole("button", { name: "Share" })).toBeEnabled());
  late(list([grant()]));
  await new Promise((r) => setTimeout(r, 20));
  expect(screen.queryByTestId("share-peer-grant")).toBeNull();
  expect(screen.queryByTestId("share-peer-existing")).toBeNull();
});

// ---- the levels are an API session's: never a terminal's (#2060 x #2061) -------------------------

test("a terminal never offers Suggest or Send, not even for a terminal grant listed above View", async () => {
  // A row an earlier or a hostile build might list: a terminal grant that says `send`. The
  // dialog reads no level into its choice, so it is still View, and a renewal asks for View.
  api.federationGrants.mockResolvedValue(
    list([grant({ session_key: TERM_KEY, kind: "terminal", level: "send", expires_at: NOW + 1800 })]),
  );
  api.changeFederationGrant.mockResolvedValue({
    grant: grant({ session_key: TERM_KEY, kind: "terminal", revision: 5 }),
    generation: 3,
  });
  const dialog = openTerminal();
  await within(dialog).findByTestId("share-peer-existing");
  const access = within(dialog).getByRole("radiogroup", { name: "Access" });
  expect(within(access).getAllByRole("radio").map((r) => r.dataset.level)).toEqual(["view"]);
  expect(within(dialog).queryByTestId("share-peer-risk")).toBeNull();
  expect(within(dialog).queryByRole("checkbox")).toBeNull();
  expect(within(dialog).queryByTestId("share-peer-suggest-line")).toBeNull();
  await userEvent.click(within(dialog).getByTestId("share-peer-go"));
  await waitFor(() =>
    expect(api.changeFederationGrant).toHaveBeenCalledWith("g7Jx9aBcDeFgHiJkLmNoPq", {
      level: "view",
      expires_in: 7 * 86400,
      revision: 4,
    }),
  );
  const body = api.changeFederationGrant.mock.calls[0][1] as Record<string, unknown>;
  expect("accept_risk" in body).toBe(false);
  expect(api.createFederationGrant).not.toHaveBeenCalled();
});

test("a terminal under StrictMode: still View alone, and the request is View without accept_risk", async () => {
  api.createFederationGrant.mockResolvedValue({
    grant: grant({ session_key: TERM_KEY, kind: "terminal", expires_at: NOW + 86400 }),
    generation: 2,
  });
  render(
    <StrictMode>
      <MemoryRouter>
        <SharePeerModal kind="terminal" sessionKey={TERM_KEY} title="Deploy" agent="claude" onClose={vi.fn()} />
      </MemoryRouter>
    </StrictMode>,
  );
  const dialog = screen.getByRole("dialog", { name: "Share with a peer" });
  await within(dialog).findByRole("radio", { name: /gpu-box/ });
  expect(within(within(dialog).getByRole("radiogroup", { name: "Access" })).getAllByRole("radio")).toHaveLength(1);
  await userEvent.click(within(dialog).getByRole("radio", { name: "1 day" }));
  await userEvent.click(within(dialog).getByTestId("share-peer-go"));
  await waitFor(() =>
    expect(api.createFederationGrant).toHaveBeenCalledWith({
      session_key: TERM_KEY,
      peer_id: GPU,
      level: "view",
      expires_in: 86400,
    }),
  );
});

// ---- StrictMode: the level section (#2061) --------------------------------------------------------

test("under StrictMode the levels work: Send's risk, the tick, accept_risk, and the countdown", async () => {
  api.federationGrants.mockResolvedValue(
    list([grant({ grant_id: "a".repeat(22), peer_id: LAPTOP, peer_name: "laptop", level: "send", expires_at: NOW + 42 * 60 + 20 })]),
  );
  api.createFederationGrant.mockResolvedValue({
    grant: grant({ level: "send", expires_at: NOW + 3600 }),
    generation: 2,
  });
  render(
    <StrictMode>
      <MemoryRouter>
        <SharePeerModal sessionKey={KEY} title="Refactor the importer" agent="API" onClose={vi.fn()} />
      </MemoryRouter>
    </StrictMode>,
  );
  await screen.findByRole("radiogroup", { name: "Peer" });
  expect(await screen.findByTestId("share-peer-grant")).toHaveTextContent(/laptop\s*Send\s*ends in 42 min/);
  expect(level("View")).toHaveAttribute("aria-checked", "true"); // never Send, also here
  await userEvent.click(level("Send"));
  expect(screen.getByTestId("share-peer-risk")).toBeInTheDocument();
  expect(share()).toBeDisabled();
  await userEvent.click(ack());
  await userEvent.click(share());
  await waitFor(() =>
    expect(api.createFederationGrant).toHaveBeenCalledWith({
      session_key: KEY,
      peer_id: GPU,
      level: "send",
      expires_in: 3600,
      accept_risk: true,
    }),
  );
  expect(await screen.findByTestId("share-peer-done")).toHaveTextContent(/^Shared with gpu-box \(Send\) · ends in /);
  await waitFor(() => expect(ack()).not.toBeChecked()); // one tick, one grant
});

test("under StrictMode a 503 busy revoke shows its retry state, never success", async () => {
  api.federationGrants.mockResolvedValue(list([grant({ level: "send", expires_at: NOW + 1800 })]));
  api.revokeFederationGrant.mockRejectedValueOnce(new ApiError(503, "busy", { code: "busy" }));
  render(
    <StrictMode>
      <MemoryRouter>
        <SharePeerModal sessionKey={KEY} title="Refactor the importer" agent="API" onClose={vi.fn()} />
      </MemoryRouter>
    </StrictMode>,
  );
  await userEvent.click(await screen.findByRole("button", { name: "Revoke gpu-box" }));
  expect(await screen.findByTestId("share-peer-busy")).toHaveTextContent(
    "A message is being delivered. Nothing changed. Try again.",
  );
  expect(screen.queryByTestId("share-peer-done")).toBeNull();
});

// ---- a grant whose numbers are not a date ----------------------------------------------------------

test.each([1e300, Number.MAX_VALUE, Infinity, NaN, -1, 0])(
  "an existing share ending at %s reads as an unknown date, never a countdown or a throw",
  async (expires_at) => {
    api.federationGrants.mockResolvedValue(list([grant({ expires_at })]));
    open();
    const row = await screen.findByTestId("share-peer-grant");
    expect(row).toHaveTextContent(/gpu-box\s*View\s*until an unknown date/);
    expect(screen.getByTestId("share-peer-existing")).toHaveTextContent("until an unknown date");
  },
);

// ---- a peer's request for more access (#2062) -----------------------------------------------------

const request = (over = {}) => ({
  request_id: "r".repeat(22),
  grant_id: "g7Jx9aBcDeFgHiJkLmNoPq",
  session_key: KEY,
  peer_id: GPU,
  peer_name: "gpu-box",
  level: "send",
  note: "Please let me approve the deploy.",
  state: "pending",
  created_at: NOW - 60,
  updated_at: NOW - 60,
  expires_at: NOW + 86400,
  ...over,
});

test("an API session's dialog shows a pending request as a card, its note as plain text", async () => {
  api.federationAccessRequests.mockResolvedValue({ access_requests: [request({ note: "<b>hi</b> [x](http://e.vil)" })] });
  open();
  const card = await screen.findByTestId("access-request");
  expect(within(card).getByTestId("access-request-asks")).toHaveTextContent("gpu-box asks to be able to send messages.");
  const quote = within(card).getByTestId("access-request-note");
  expect(quote).toHaveTextContent("<b>hi</b> [x](http://e.vil)");
  expect(quote.querySelector("b, a")).toBeNull();
  expect(api.federationAccessRequests).toHaveBeenCalledWith(KEY);
});

test.each([1e300, Number.NaN, Number.POSITIVE_INFINITY, -1, "never"])(
  "a request whose times are %s is still its card, and the dialog does not throw",
  async (t) => {
    api.federationAccessRequests.mockResolvedValue({
      access_requests: [request({ created_at: t, updated_at: t, expires_at: t })],
    });
    open();
    expect(await screen.findByTestId("access-request")).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "Peer" })).toBeInTheDocument();
  },
);

test("a terminal's dialog asks for no access requests and shows no card, whatever the server says", async () => {
  api.federationAccessRequests.mockResolvedValue({ access_requests: [request()] });
  openTerminal();
  await screen.findByTestId("share-peer-limit");
  await waitFor(() => expect(api.federationGrants).toHaveBeenCalled());
  await new Promise((r) => setTimeout(r, 20));
  expect(screen.queryByTestId("access-request")).toBeNull();
  expect(api.federationAccessRequests).not.toHaveBeenCalled();
});

test("under StrictMode a pending request's card is read and shown", async () => {
  api.federationAccessRequests.mockResolvedValue({ access_requests: [request()] });
  render(
    <StrictMode>
      <MemoryRouter>
        <SharePeerModal sessionKey={KEY} title="Refactor the importer" agent="API" onClose={vi.fn()} />
      </MemoryRouter>
    </StrictMode>,
  );
  expect(await screen.findByTestId("access-request")).toBeInTheDocument();
});
