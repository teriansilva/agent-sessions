import { act, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { remotePath, REMOTE_ROUTE } from "../../lib/federation";

// The route `/remote/:peerId/:grantId` picks the pane by the listed kind (#2060). A terminal
// share must never be drawn by the conversation pane (`RemotePane`) and an API share never by
// the screen pane (`RemoteScreenPane`): each pane's reads are the other kind's refusals, so the
// wrong pane would call a share that exists "no longer available". These tests hold the panes'
// reads as spies and assert which of them was ever asked.
const api = vi.hoisted(() => ({
  federationRemote: vi.fn(),
  federationRemoteSnapshot: vi.fn(),
  federationRemoteEvents: vi.fn(),
  federationRemoteHistory: vi.fn(),
  federationRemoteFrame: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  api,
}));

const { RemoteSessionView } = await import("./RemoteSessionView");
const { refreshRemoteShares, resetRemoteSharesForTest } = await import("../../app/remoteShares");

const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const GRANT = "g7Jx9aBcDeFgHiJkLmNoPq";
const NOV_22 = Date.UTC(2026, 10, 22, 12) / 1000;

const listed = (kind?: string) => ({
  key: `peer:${GPU}/claude:7c1e3d2f-9a4b-4d8e-8f32-7b5c4d3e2f10`,
  peer_id: GPU,
  peer_name: "gpu-box",
  connected: true,
  grant_id: GRANT,
  engine: "claude",
  native_id: "7c1e3d2f-9a4b-4d8e-8f32-7b5c4d3e2f10",
  title: "Deploy the staging stack",
  ...(kind === undefined ? {} : { kind }),
  level: "view",
  expires_at: NOV_22,
  revision: 2,
  seen_at: NOV_22 - 60,
});

const held = (_a: unknown, _b: unknown, _c: unknown, _d: unknown, signal?: AbortSignal) =>
  new Promise((_resolve, reject) => signal?.addEventListener("abort", () => reject(new Error("aborted"))));

beforeEach(() => {
  resetRemoteSharesForTest();
  Object.values(api).forEach((f) => f.mockReset());
  api.federationRemote.mockResolvedValue({ remote: [] });
  api.federationRemoteSnapshot.mockImplementation(() => new Promise(() => {}));
  api.federationRemoteEvents.mockImplementation(held);
  api.federationRemoteHistory.mockImplementation(() => new Promise(() => {}));
  api.federationRemoteFrame.mockImplementation(held);
});

function open(strict = false) {
  const tree = (
    <MemoryRouter initialEntries={[remotePath(GPU, GRANT)]}>
      <Routes>
        <Route path={REMOTE_ROUTE} element={<RemoteSessionView />} />
      </Routes>
    </MemoryRouter>
  );
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}

const conversationReads = () =>
  api.federationRemoteSnapshot.mock.calls.length +
  api.federationRemoteEvents.mock.calls.length +
  api.federationRemoteHistory.mock.calls.length;

test.each([false, true])(
  "a terminal share opens the screen pane and never the conversation pane (StrictMode: %s)",
  async (strict) => {
    api.federationRemote.mockResolvedValue({ remote: [listed("terminal")] });
    open(strict);
    expect(await screen.findByTestId("remote-screen-bar")).toBeInTheDocument();
    expect(screen.getByTestId("screen-tag")).toBeInTheDocument();
    await waitFor(() => expect(api.federationRemoteFrame).toHaveBeenCalled());
    expect(api.federationRemoteFrame.mock.calls[0].slice(0, 2)).toEqual([GPU, GRANT]);
    expect(conversationReads()).toBe(0);
    expect(screen.queryByTestId("remote-view-only")).toBeNull();
  },
);

test.each([["api"], [undefined]])(
  "an API share (kind %s) opens the conversation pane and never the screen pane",
  async (kind) => {
    api.federationRemote.mockResolvedValue({ remote: [listed(kind)] });
    open();
    await waitFor(() => expect(api.federationRemoteSnapshot).toHaveBeenCalled());
    expect(screen.queryByTestId("remote-screen-bar")).toBeNull();
    expect(screen.queryByTestId("screen-tag")).toBeNull();
    expect(api.federationRemoteFrame).not.toHaveBeenCalled();
  },
);

test("a share of a kind this build does not know is not listed, and no pane reads it as a terminal", async () => {
  api.federationRemote.mockResolvedValue({ remote: [listed("byte_stream")] });
  open();
  // Not listed: the conversation pane's fallback, which the server answers 404 for a share the
  // mirror does not have as an API share. The screen pane is never asked.
  await waitFor(() => expect(api.federationRemoteSnapshot).toHaveBeenCalled());
  expect(api.federationRemoteFrame).not.toHaveBeenCalled();
});

test("a fallback conversation pane gives way to the screen pane once the list names a terminal", async () => {
  // The first list read does not have the share yet: the route falls back to the conversation
  // pane, as it always did. When the list then names it a terminal, that word decides.
  open();
  await waitFor(() => expect(api.federationRemoteSnapshot).toHaveBeenCalled());
  expect(screen.queryByTestId("remote-screen-bar")).toBeNull();
  api.federationRemote.mockResolvedValue({ remote: [listed("terminal")] });
  await act(() => refreshRemoteShares());
  expect(await screen.findByTestId("remote-screen-bar")).toBeInTheDocument();
  await waitFor(() => expect(api.federationRemoteFrame).toHaveBeenCalled());
});

test("a listed terminal whose row leaves the list stays the screen pane: it never turns into a conversation", async () => {
  api.federationRemote.mockResolvedValue({ remote: [listed("terminal")] });
  open();
  expect(await screen.findByTestId("remote-screen-bar")).toBeInTheDocument();
  api.federationRemote.mockResolvedValue({ remote: [] });
  await act(() => refreshRemoteShares());
  expect(screen.getByTestId("screen-tag")).toBeInTheDocument();
  expect(conversationReads()).toBe(0);
});

test("a listed API share whose row leaves the list stays the conversation pane", async () => {
  api.federationRemote.mockResolvedValue({ remote: [listed("api")] });
  open();
  await waitFor(() => expect(api.federationRemoteSnapshot).toHaveBeenCalled());
  api.federationRemote.mockResolvedValue({ remote: [] });
  await act(() => refreshRemoteShares());
  // Even if the same grant id were later listed as a terminal, the listed choice is kept.
  api.federationRemote.mockResolvedValue({ remote: [listed("terminal")] });
  await act(() => refreshRemoteShares());
  expect(screen.queryByTestId("remote-screen-bar")).toBeNull();
  expect(api.federationRemoteFrame).not.toHaveBeenCalled();
});
