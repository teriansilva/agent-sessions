import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { beforeEach, expect, test, vi } from "vitest";

// What is shared with one peer, under its row in Settings → Peers (#2055). Layout and the page
// around it are the browser spec's (e2e/share-peer.spec.ts); this pins the loading guard.
const api = vi.hoisted(() => ({
  federationGrantsToPeer: vi.fn(),
  revokeFederationGrant: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  api,
}));

const { PeerShares } = await import("./PeerShares");

const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const LAPTOP = "b7nd4qzse2vkx6mhp3ytw5caj4ruo2gf";
const NOW = Date.now() / 1000;

const grant = (over = {}) => ({
  grant_id: "g7Jx9aBcDeFgHiJkLmNoPq",
  session_key: "codex-api:0d0f6a52-1111-4222-8333-444455556666",
  peer_id: GPU,
  peer_name: "gpu-box",
  level: "view",
  expires_at: NOW + 3 * 86400,
  revision: 4,
  state: "live",
  created_at: NOW - 86400,
  ended_at: null,
  end_reason: null,
  title: "Refactor the importer",
  ...over,
});
const list = (grants: unknown[]) => ({
  grants,
  generation: 1,
  levels: ["view"],
  default_expires_in: { view: 604800 },
  max_expires_in: 7776000,
});

const shown = (peerId: string, peerName: string) => (
  <StrictMode>
    <PeerShares peerId={peerId} peerName={peerName} remote={[]} />
  </StrictMode>
);

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
});

// #2055 review (P2): the app mounts under <StrictMode> (main.tsx). Its effect replay ran the old
// `alive` ref's cleanup and nothing set it again, so the outgoing list never left "0 shared".
test("under StrictMode the outgoing list loads and counts", async () => {
  api.federationGrantsToPeer.mockResolvedValue(list([grant()]));
  render(shown(GPU, "gpu-box"));
  await waitFor(() =>
    expect(screen.getByTestId("peer-shares-counts")).toHaveTextContent("1 shared · 0 received"),
  );
  const row = within(screen.getByTestId("peer-shares-out")).getByTestId("peer-grant");
  expect(row).toHaveTextContent(/Refactor the importer\s*View\s*until/);
});

test("under StrictMode Revoke ends the share and the list is read again", async () => {
  api.federationGrantsToPeer.mockResolvedValue(list([grant()]));
  api.revokeFederationGrant.mockResolvedValue({ grant: grant({ state: "revoked" }), generation: 2 });
  render(shown(GPU, "gpu-box"));
  const revoke = await screen.findByRole("button", {
    name: "Revoke Refactor the importer for gpu-box",
    hidden: true,
  });
  api.federationGrantsToPeer.mockResolvedValue(list([]));
  await userEvent.click(revoke);
  await waitFor(() =>
    expect(screen.getByTestId("peer-shares-counts")).toHaveTextContent("0 shared · 0 received"),
  );
  expect(api.revokeFederationGrant).toHaveBeenCalledWith("g7Jx9aBcDeFgHiJkLmNoPq");
});

test("a late answer for the peer shown before is dropped", async () => {
  let late: (v: unknown) => void = () => {};
  api.federationGrantsToPeer.mockImplementation((peerId: string) =>
    peerId === GPU ? new Promise((resolve) => (late = resolve)) : Promise.resolve(list([])),
  );
  const { rerender } = render(shown(GPU, "gpu-box"));
  rerender(shown(LAPTOP, "laptop"));
  await waitFor(() =>
    expect(screen.getByTestId("peer-shares-counts")).toHaveTextContent("0 shared · 0 received"),
  );
  late(list([grant()]));
  await new Promise((r) => setTimeout(r, 20));
  expect(screen.getByTestId("peer-shares-counts")).toHaveTextContent("0 shared · 0 received");
  expect(screen.queryByTestId("peer-grant")).toBeNull();
});
