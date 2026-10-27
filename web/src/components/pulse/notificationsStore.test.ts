/** The shared bell read (#1736) never carries one session's notifications into the next.
 *
 *  The relay shell outlives the app it mounts: sign out tears the app down (the last reader
 *  leaves) and a new sign-in mounts another one in the same tab. A bell action's answer that was
 *  in flight across that boundary belongs to the session that is gone (Hermes 7183). */
import { afterEach, expect, test, vi } from "vitest";

vi.mock("../../lib/api", () => ({ api: { notifications: vi.fn() } }));

import { api } from "../../lib/api";
import type { PulseNotification } from "../../types/api";
import {
  type NotificationsAnswer,
  getNotifications,
  mutateNotifications,
  subscribeNotifications,
} from "./notificationsStore";

const row = (title: string) => ({ id: title, title, read: false }) as unknown as PulseNotification;

function deferred() {
  let resolve: (v: NotificationsAnswer) => void = () => {};
  const promise = new Promise<NotificationsAnswer>((r) => (resolve = r));
  return { promise, resolve };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

afterEach(() => vi.clearAllMocks());

test("an action's answer arriving after sign-out and a new session's mount is dropped", async () => {
  vi.mocked(api.notifications).mockResolvedValue({ notifications: [row("first operator's alert")], unread: 1 });
  const leave = subscribeNotifications(() => {});
  await flush();
  expect(getNotifications().items.map((n) => n.title)).toEqual(["first operator's alert"]);

  const late = deferred();
  const action = mutateNotifications(() => late.promise, false);
  leave(); // sign out: the app unmounts, the store forgets
  expect(getNotifications().items).toEqual([]);

  // The next session mounts in the same tab, and its first read fails.
  vi.mocked(api.notifications).mockRejectedValue(new Error("offline"));
  const leaveNext = subscribeNotifications(() => {});
  await flush();

  late.resolve({ notifications: [row("first operator's alert")], unread: 1 });
  await action;
  expect(getNotifications().items).toEqual([]);
  expect(getNotifications().unread).toBe(0);
  leaveNext();
});

test("an action's answer inside the same session applies", async () => {
  vi.mocked(api.notifications).mockResolvedValue({ notifications: [row("a")], unread: 1 });
  const leave = subscribeNotifications(() => {});
  await flush();
  await mutateNotifications(async () => ({ notifications: [], unread: 0 }), false);
  expect(getNotifications().unread).toBe(0);
  expect(getNotifications().items).toEqual([]);
  leave();
});

test("a failed action rejects, so the bell keeps what it shows", async () => {
  vi.mocked(api.notifications).mockResolvedValue({ notifications: [row("a")], unread: 1 });
  const leave = subscribeNotifications(() => {});
  await flush();
  await expect(mutateNotifications(() => Promise.reject(new Error("500")), true)).rejects.toThrow();
  expect(getNotifications().unread).toBe(1);
  leave();
});
