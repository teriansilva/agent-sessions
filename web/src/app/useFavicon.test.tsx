/** The favicon's four states (#1736): relay × unread, each its own file under the build's base,
 *  read from the same stores the bell badge and the HQ dot read, with no poll of its own. */
import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("../lib/api", () => ({
  api: {
    notifications: vi.fn(),
    askUnread: vi.fn(),
  },
}));

import { api } from "../lib/api";
import { useAskUnread } from "../components/ask/askUnreadStore";
import { ASK_SEEN_EVENT } from "../components/ask/askUpdates";
import { useNotifications } from "../components/pulse/notificationsStore";
import { ACTION_RESOLVED_EVENT } from "../lib/actionEvents";
import { clearRelay, publishRelay } from "../homefree/relayStatus";
import { faviconHref, useFavicon } from "./useFavicon";

function Probe() {
  useFavicon();
  return null;
}

const icon = () => document.head.querySelector<HTMLLinkElement>('link[rel~="icon"]')!;

function unread(bell: number, ask: boolean) {
  vi.mocked(api.notifications).mockResolvedValue({ notifications: [], unread: bell });
  vi.mocked(api.askUnread).mockResolvedValue({ unread: ask, conversations: ask ? 1 : 0 });
}

beforeEach(() => {
  document.head.innerHTML = '<link rel="icon" type="image/svg+xml" href="/favicon.svg" />';
  unread(0, false);
});

afterEach(() => {
  clearRelay();
  vi.unstubAllEnvs();
  vi.clearAllMocks();
});

test.each([
  ["/", false, false, "/favicon.svg"],
  ["/", false, true, "/favicon-unread.svg"],
  ["/", true, false, "/favicon-relay.svg"],
  ["/", true, true, "/favicon-relay-unread.svg"],
  ["/connect/", false, false, "/connect/favicon.svg"],
  ["/connect/", false, true, "/connect/favicon-unread.svg"],
  ["/connect/", true, false, "/connect/favicon-relay.svg"],
  ["/connect/", true, true, "/connect/favicon-relay-unread.svg"],
])("base %s, relay=%s, unread=%s → %s", (base, relay, isUnread, href) => {
  expect(faviconHref(relay, isUnread, base)).toBe(href);
});

test("the build's base is the default (the relay build is /connect/)", () => {
  vi.stubEnv("BASE_URL", "/connect/");
  expect(faviconHref(true, false)).toBe("/connect/favicon-relay.svg");
});

test("an unread bell notification adds the dot; reading it takes it away", async () => {
  unread(2, false);
  render(<Probe />);
  await waitFor(() => expect(icon().getAttribute("href")).toBe("/favicon-unread.svg"));
  unread(0, false);
  act(() => {
    window.dispatchEvent(new CustomEvent(ACTION_RESOLVED_EVENT));
  });
  await waitFor(() => expect(icon().getAttribute("href")).toBe("/favicon.svg"));
});

test("an unread HQ reply adds the dot; marking it seen takes it away", async () => {
  unread(0, true);
  render(<Probe />);
  await waitFor(() => expect(icon().getAttribute("href")).toBe("/favicon-unread.svg"));
  unread(0, false);
  act(() => {
    window.dispatchEvent(new CustomEvent(ASK_SEEN_EVENT));
  });
  await waitFor(() => expect(icon().getAttribute("href")).toBe("/favicon.svg"));
});

test("through the relay the icon is the relay mark, with the dot when unread, under /connect/", async () => {
  vi.stubEnv("BASE_URL", "/connect/");
  document.head.innerHTML = '<link rel="icon" type="image/svg+xml" href="/connect/favicon-relay.svg" />';
  publishRelay({ link: "linked", name: "box", status: "connected", remaining: "" }, () => {});
  const { unmount } = render(<Probe />);
  await waitFor(() => expect(api.askUnread).toHaveBeenCalled());
  expect(icon().getAttribute("href")).toBe("/connect/favicon-relay.svg");
  unread(1, false);
  act(() => {
    window.dispatchEvent(new CustomEvent(ACTION_RESOLVED_EVENT));
  });
  await waitFor(() => expect(icon().getAttribute("href")).toBe("/connect/favicon-relay-unread.svg"));
  // The app unmounts (sign out): the shell's own icon comes back.
  unmount();
  expect(icon().getAttribute("href")).toBe("/connect/favicon-relay.svg");
});

test("a page without an icon link gets one", async () => {
  document.head.innerHTML = "";
  unread(1, false);
  render(<Probe />);
  await waitFor(() => expect(icon()?.getAttribute("href")).toBe("/favicon-unread.svg"));
});

/** The bell's and the HQ icon's own reads, mounted beside the favicon. */
function TopBarReaders() {
  useNotifications();
  useAskUnread();
  return null;
}

test("no poll of its own: beside the bell and the HQ icon, each endpoint is read once", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    render(
      <>
        <TopBarReaders />
        <Probe />
      </>,
    );
    await waitFor(() => expect(api.askUnread).toHaveBeenCalledTimes(1));
    expect(api.notifications).toHaveBeenCalledTimes(1);
    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    expect(api.notifications).toHaveBeenCalledTimes(2);
    expect(api.askUnread).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});
