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

function open(onClose = vi.fn()) {
  render(
    <MemoryRouter>
      <SharePeerModal sessionKey={KEY} title="Refactor the importer" agent="API" onClose={onClose} />
    </MemoryRouter>,
  );
  return onClose;
}

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
  expect(access[1]).toBeDisabled();
  expect(access[1]).toHaveTextContent(/Suggest\s*later/);
  expect(access[2]).toBeDisabled();
  expect(access[2]).toHaveTextContent(/Send\s*later/);

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
      expires_in: 30 * 86400,
    }),
  );
  expect(api.changeFederationGrant).not.toHaveBeenCalled();
  const row = await screen.findByTestId("share-peer-grant");
  expect(row).toHaveTextContent(/laptop\s*View\s*until/);
  expect(screen.getByRole("heading", { name: "Shared with · 1" })).toBeInTheDocument();
  expect(screen.getByTestId("share-peer-done")).toHaveTextContent(/^Shared with laptop until /);
});

test("sharing again with the same peer changes the existing grant with its revision", async () => {
  api.federationGrants.mockResolvedValue(list([grant()]));
  api.changeFederationGrant.mockResolvedValue({ grant: grant({ revision: 5 }), generation: 3 });
  open();
  expect(await screen.findByTestId("share-peer-existing")).toHaveTextContent(
    /Already shared with gpu-box until .*Sharing again changes when it ends\./,
  );
  await userEvent.click(screen.getByRole("radio", { name: "90 days" }));
  await userEvent.click(screen.getByRole("button", { name: "Share" }));
  await waitFor(() =>
    expect(api.changeFederationGrant).toHaveBeenCalledWith("g7Jx9aBcDeFgHiJkLmNoPq", 90 * 86400, 4),
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
  const onClose = open();
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
  const onClose = open();
  await screen.findByRole("radiogroup", { name: "Peer" });
  await userEvent.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalledTimes(1);
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onClose).toHaveBeenCalledTimes(2);
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
  expect(await screen.findByTestId("share-peer-done")).toHaveTextContent(/^Shared with gpu-box until /);
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
