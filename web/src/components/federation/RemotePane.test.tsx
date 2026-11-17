import { render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

// The read-only pane under <StrictMode> (main.tsx), checked with the share dialog's fix (#2055
// review, P2): its reads are fenced by a per-effect AbortController and the reducer's
// generation, not by a ref the replay's cleanup leaves false, so the replayed mount reads again
// and shows the answer. The journey itself is the browser spec's (e2e/share-peer.spec.ts).
const api = vi.hoisted(() => ({
  federationRemote: vi.fn(),
  federationRemoteSnapshot: vi.fn(),
  federationRemoteEvents: vi.fn(),
  federationRemoteHistory: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  api,
}));

const { RemotePane } = await import("./RemotePane");
const { resetRemoteSharesForTest } = await import("../../app/remoteShares");

const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const GRANT = "g7Jx9aBcDeFgHiJkLmNoPq";

beforeEach(() => {
  resetRemoteSharesForTest();
  Object.values(api).forEach((f) => f.mockReset());
  api.federationRemote.mockResolvedValue({ remote: [] });
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
  // Held until the read is given up, like the owner's long-poll.
  api.federationRemoteEvents.mockImplementation(
    (_p: string, _g: string, _a: number, _w: number, signal?: AbortSignal) =>
      new Promise((_resolve, reject) => signal?.addEventListener("abort", () => reject(new Error("aborted")))),
  );
});

test("under StrictMode the pane reads the snapshot and shows its turns", async () => {
  render(
    <StrictMode>
      <MemoryRouter>
        <RemotePane peerId={GPU} grantId={GRANT} />
      </MemoryRouter>
    </StrictMode>,
  );
  expect(await screen.findByText("One lock for the whole batch.")).toBeInTheDocument();
  expect(screen.getByTestId("remote-state")).toHaveTextContent("connected");
  expect(screen.queryByText("Loading the conversation…")).toBeNull();
});
