import { render, screen } from "@testing-library/react";
import type { NodeProps } from "@xyflow/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, onTestFinished, test, vi } from "vitest";
import { UNKNOWN_DATE, type RemoteShare } from "../../lib/federation";

// #2055 review (P1): a paired peer could list an expiry such as 1e300. The receiving server
// accepted it, the browser kept it, and building the "until 22 Nov" date threw
// `RangeError: Invalid time value` — in the session list's remote row, which is drawn outside the
// main pane's error boundary, so the whole UI went down without the share ever being opened.
// The server refuses such a listing now and the list read drops such a row, but a value already
// held must still never throw: every surface that shows an expiry reads it as a neutral text.
// These are the three surfaces the review named: the list row, the map node, the pane's banner.
const shares = vi.hoisted(() => ({ list: [] as unknown[] }));
vi.mock("../../app/remoteShares", () => ({
  useRemoteShares: () => shares.list,
  refreshRemoteShares: () => Promise.resolve(true),
  // The panes' Leave action (#2201) reads the list's status as well.
  useRemoteSharesStatus: () => ({ loaded: true, attempts: 1 }),
  remoteSharesNow: () => ({ rows: shares.list, loaded: true }),
  proveLeft: () => Promise.resolve(true),
}));
const api = vi.hoisted(() => ({
  federationRemoteSnapshot: vi.fn(),
  federationRemoteEvents: vi.fn(),
  federationRemoteHistory: vi.fn(),
  federationRemoteFrame: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  api,
}));

const { RemoteRow } = await import("./RemoteRow");
const { RemotePane } = await import("./RemotePane");
const { RemoteScreenPane } = await import("./RemoteScreenPane");
const { RemoteNode } = await import("../overview/RemoteNode");

const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const GRANT = "g7Jx9aBcDeFgHiJkLmNoPq";
const HOSTILE = [1e300, Number.MAX_VALUE, Infinity, NaN, -1];

const share = (expires_at: number): RemoteShare => ({
  key: `peer:${GPU}/codex-api:0d0f6a52-1111-4222-8333-444455556666`,
  peer_id: GPU,
  peer_name: "gpu-box",
  connected: true,
  grant_id: GRANT,
  engine: "codex-api",
  native_id: "0d0f6a52-1111-4222-8333-444455556666",
  title: "Tune the render queue",
  level: "view",
  expires_at,
  revision: 3,
  seen_at: 1_800_000_000,
});

beforeEach(() => {
  shares.list = [];
  Object.values(api).forEach((f) => f.mockReset());
  // The pane stays at "loading": what is under test is its banner, not its reads.
  api.federationRemoteSnapshot.mockReturnValue(new Promise(() => {}));
  api.federationRemoteFrame.mockReturnValue(new Promise(() => {}));
});

test.each(HOSTILE)("the session list's remote row shows %s as an unknown date", (expires_at) => {
  render(
    <MemoryRouter>
      <ul>
        <RemoteRow remote={share(expires_at)} />
      </ul>
    </MemoryRouter>,
  );
  expect(screen.getByTestId("remote-row-meta")).toHaveTextContent(
    `view only · until ${UNKNOWN_DATE}`,
  );
});

test.each(HOSTILE)("the map's remote node shows %s as an unknown date", (expires_at) => {
  const props = { data: { remote: share(expires_at) } } as unknown as NodeProps;
  render(<RemoteNode {...props} />);
  expect(screen.getByTestId("remote-node")).toHaveTextContent(`until ${UNKNOWN_DATE}`);
});

test.each(HOSTILE)("the read-only pane's banner shows %s as an unknown date", (expires_at) => {
  shares.list = [share(expires_at)];
  render(
    <MemoryRouter>
      <RemotePane peerId={GPU} grantId={GRANT} />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("remote-banner")).toHaveTextContent(
    `Shared by gpu-box · view only · until ${UNKNOWN_DATE}`,
  );
});

// The same surfaces for a shared terminal (#2060): its row, its node and the screen pane's banner.
const terminal = (expires_at: number): RemoteShare => ({ ...share(expires_at), kind: "terminal" });

test.each(HOSTILE)("a terminal's row and node show %s as an unknown date", (expires_at) => {
  render(
    <MemoryRouter>
      <ul>
        <RemoteRow remote={terminal(expires_at)} />
      </ul>
      <RemoteNode {...({ data: { remote: terminal(expires_at) } } as unknown as NodeProps)} />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("remote-row-meta")).toHaveTextContent(`until ${UNKNOWN_DATE}`);
  expect(screen.getByTestId("remote-node")).toHaveTextContent(`until ${UNKNOWN_DATE}`);
});

test.each(HOSTILE)("the screen pane's banner shows %s as an unknown date", (expires_at) => {
  shares.list = [terminal(expires_at)];
  render(
    <MemoryRouter>
      <RemoteScreenPane peerId={GPU} grantId={GRANT} />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("remote-banner")).toHaveTextContent(
    `Shared by gpu-box · screen only · until ${UNKNOWN_DATE}`,
  );
});

test("a real expiry still reads as its date on all three", () => {
  const nov22 = Date.UTC(2026, 10, 22, 12) / 1000;
  // The clock is pinned weeks before the expiry: within 24 h the surfaces read "ends in …"
  // instead of the date, so against the real clock this test started failing at 21 Nov 12:00 UTC.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(Date.UTC(2026, 10, 1, 12)));
  onTestFinished(() => {
    vi.useRealTimers();
  });
  shares.list = [share(nov22)];
  render(
    <MemoryRouter>
      <ul>
        <RemoteRow remote={share(nov22)} />
      </ul>
      <RemoteNode {...({ data: { remote: share(nov22) } } as unknown as NodeProps)} />
      <RemotePane peerId={GPU} grantId={GRANT} />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("remote-row-meta")).toHaveTextContent("view only · until 22 Nov");
  expect(screen.getByTestId("remote-node")).toHaveTextContent("until 22 Nov");
  expect(screen.getByTestId("remote-banner")).toHaveTextContent("view only · until 22 Nov");
});
