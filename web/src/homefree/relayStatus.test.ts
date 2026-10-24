import { afterEach, expect, test, vi } from "vitest";

import {
  clearRelay,
  dockRelayPlate,
  getRelaySnapshot,
  minutesLeft,
  onRelayDockChange,
  publishRelay,
  relayPlateDocked,
  relaySignOut,
  subscribeRelay,
  updateRelay,
  type RelaySnapshot,
} from "./relayStatus";

const LINKED: RelaySnapshot = {
  link: "linked",
  name: "onyx-9780",
  status: "connected — streaming your BattleLab, the relay is blind",
  remaining: "3:57:50",
};

afterEach(() => clearRelay());

test("nothing is published outside the relay: the snapshot is null and an update changes nothing", () => {
  const seen = vi.fn();
  const off = subscribeRelay(seen);
  updateRelay({ link: "ending" });
  expect(getRelaySnapshot()).toBeNull();
  expect(seen).not.toHaveBeenCalled();
  off();
});

test("publish, update and clear reach subscribers; an unchanged update is not a change", () => {
  const seen = vi.fn();
  const off = subscribeRelay(seen);
  publishRelay(LINKED, () => {});
  expect(getRelaySnapshot()).toEqual(LINKED);
  updateRelay({ link: "ending", status: "session ends soon (120s) — reconnect after" });
  expect(getRelaySnapshot()).toMatchObject({ link: "ending", name: "onyx-9780" });
  updateRelay({ link: "ending" }); // same value: no re-render
  expect(seen).toHaveBeenCalledTimes(2);
  clearRelay();
  expect(getRelaySnapshot()).toBeNull();
  expect(seen).toHaveBeenCalledTimes(3);
  off();
});

test("Sign out runs the shell's path while published, and is a no-op once the attempt ended", () => {
  const signOut = vi.fn();
  publishRelay(LINKED, signOut);
  relaySignOut();
  expect(signOut).toHaveBeenCalledTimes(1);
  clearRelay();
  relaySignOut(); // a stale plate cannot end a newer session
  expect(signOut).toHaveBeenCalledTimes(1);
});

test("a cleared store ignores a late update — an ended attempt cannot repaint the plate", () => {
  publishRelay(LINKED, () => {});
  clearRelay();
  updateRelay({ remaining: "3:57:49" });
  expect(getRelaySnapshot()).toBeNull();
});

test("the dock tells the shell when the first plate docks and the last one leaves, once each", () => {
  const changes: boolean[] = [];
  const off = onRelayDockChange((d) => changes.push(d));
  const undockA = dockRelayPlate();
  const undockB = dockRelayPlate();
  expect(relayPlateDocked()).toBe(true);
  undockA();
  undockA(); // idempotent: StrictMode's double cleanup must not under-count
  expect(relayPlateDocked()).toBe(true);
  undockB();
  expect(relayPlateDocked()).toBe(false);
  expect(changes).toEqual([true, false]);
  off();
});

test("time left to the minute: what the plate's description and tooltip say", () => {
  expect(minutesLeft("3:57:50")).toBe("3 h 57 min left");
  expect(minutesLeft("1:00:59")).toBe("1 h 0 min left");
  expect(minutesLeft("04:59")).toBe("4 min left");
  expect(minutesLeft("00:42")).toBe("under a minute left");
  expect(minutesLeft("")).toBe("");
  expect(minutesLeft("--:--")).toBe("");
});
