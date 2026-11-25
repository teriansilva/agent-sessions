import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PushDevices } from "./PushDevices";

const pushKey = vi.fn();
const pushSubscribe = vi.fn();
const pushUnsubscribe = vi.fn();

vi.mock("../../lib/api", () => ({
  api: {
    pushKey: () => pushKey(),
    pushSubscribe: (s: unknown) => pushSubscribe(s),
    pushUnsubscribe: (id: string) => pushUnsubscribe(id),
  },
}));

const subscribe = vi.fn();
const getSubscription = vi.fn();

function installPushEnv(permission: NotificationPermission) {
  const requestPermission = vi.fn().mockResolvedValue(permission);
  vi.stubGlobal("Notification", { permission: "default", requestPermission });
  vi.stubGlobal("PushManager", class {});
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: {
      ready: Promise.resolve({ pushManager: { subscribe, getSubscription } }),
    },
  });
  return requestPermission;
}

describe("PushDevices — the browser side of Web Push (#726)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    pushKey.mockResolvedValue({ public_key: "BKxx", subscriptions: [] });
    pushSubscribe.mockResolvedValue({});
    getSubscription.mockResolvedValue(null);
    subscribe.mockResolvedValue({
      toJSON: () => ({ endpoint: "https://push.example/abc" }),
    });
  });

  it("registers this browser and hands the subscription to the server", async () => {
    const requestPermission = installPushEnv("granted");
    render(<PushDevices />);

    // Permission MUST come from a user gesture — never an effect. If this ever fires on
    // mount, browsers reject it (Safari silently) and push quietly never works.
    expect(requestPermission).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByRole("button", { name: /enable on this device/i }),
    );

    await waitFor(() => expect(pushSubscribe).toHaveBeenCalledTimes(1));
    expect(requestPermission).toHaveBeenCalledTimes(1);
    expect(subscribe).toHaveBeenCalledWith(
      expect.objectContaining({
        userVisibleOnly: true,
        applicationServerKey: "BKxx",
      }),
    );
  });

  it("reuses an existing subscription rather than minting a second endpoint", async () => {
    installPushEnv("granted");
    getSubscription.mockResolvedValue({
      toJSON: () => ({ endpoint: "https://push.example/existing" }),
    });
    render(<PushDevices />);

    fireEvent.click(
      screen.getByRole("button", { name: /enable on this device/i }),
    );

    await waitFor(() => expect(pushSubscribe).toHaveBeenCalledTimes(1));
    // Re-subscribing would orphan the row the server already has for this device.
    expect(subscribe).not.toHaveBeenCalled();
  });

  it("says plainly that a blocked permission cannot be undone from the app", async () => {
    installPushEnv("denied");
    render(<PushDevices />);

    fireEvent.click(
      screen.getByRole("button", { name: /enable on this device/i }),
    );

    await waitFor(() =>
      expect(screen.getByText(/blocked for this site/i)).toBeTruthy(),
    );
    expect(pushSubscribe).not.toHaveBeenCalled();
    // A button that silently does nothing is worse than a disabled one.
    expect(
      screen
        .getByRole("button", { name: /enable on this device/i })
        .hasAttribute("disabled"),
    ).toBe(true);
  });

  it("degrades to an explanation where Web Push is unsupported", () => {
    vi.stubGlobal("PushManager", undefined);
    vi.stubGlobal("Notification", undefined);
    render(<PushDevices />);
    expect(screen.getByText(/doesn't support web push/i)).toBeTruthy();
  });
});

describe("removing a device (#730 review)", () => {
  it("does not unsubscribe THIS browser when removing a different device", async () => {
    installPushEnv("granted");
    const unsubscribe = vi.fn().mockResolvedValue(true);
    // This browser's endpoint hashes to some id; the row being removed is a DIFFERENT one.
    getSubscription.mockResolvedValue({
      endpoint: "https://push.example/this-browser",
      toJSON: () => ({ endpoint: "https://push.example/this-browser" }),
      unsubscribe,
    });
    pushKey.mockResolvedValue({
      public_key: "BKxx",
      subscriptions: [
        {
          id: "0000000000000000",
          origin: "https://push.example",
          created_at: 1,
        },
      ],
    });

    render(<PushDevices />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /remove/i })).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: /remove/i }));

    await waitFor(() =>
      expect(pushUnsubscribe).toHaveBeenCalledWith("0000000000000000"),
    );
    // Removing a phone from the desktop must not kill desktop push — and must not leave the
    // desktop's server row pointing at an endpoint the browser has dropped.
    expect(unsubscribe).not.toHaveBeenCalled();
  });
});
