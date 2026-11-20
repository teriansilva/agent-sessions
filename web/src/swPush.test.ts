import { describe, expect, test, vi } from "vitest";

import {
  createPushHandler,
  serverRetractions,
  reconcileDeviceNotifications,
  type NotificationSurface,
} from "./swPush";

/** A notification shade: `showNotification` replaces by tag, `getNotifications` filters by tag. */
function shade() {
  const open = new Map<
    string,
    { tag: string; title: string; close: () => void }
  >();
  const shown: { title: string; tag: string; silent?: boolean }[] = [];
  let gate: Promise<void> | null = null;
  const reg: NotificationSurface = {
    showNotification: async (title, o) => {
      if (gate) await gate;
      const tag = String(o?.tag);
      shown.push({ title, tag, silent: o?.silent });
      open.set(tag, { tag, title, close: () => open.delete(tag) });
    },
    getNotifications: async (f) =>
      [...open.values()].filter(
        (n) => !f?.tag || n.tag === f.tag,
      ) as unknown as Notification[],
  };
  const hold = () => {
    let release!: () => void;
    gate = new Promise<void>((r) => (release = r));
    return () => {
      gate = null;
      release();
    };
  };
  return { reg, open, shown, hold };
}

/** A server whose retraction list the test controls. */
function server(initial: string[] = []) {
  const retracted = new Set(initial);
  const asked: string[] = [];
  const isRetracted = async (tag: string) => {
    asked.push(tag);
    return retracted.has(tag);
  };
  return { retracted, asked, isRetracted };
}

describe("push handling (#1086 Phase 4, Hermes 5265)", () => {
  test("EVERY push shows something — there is no push that shows nothing", async () => {
    const { reg, shown } = shade();
    const srv = server(["needs-you:a:1"]);
    const push = createPushHandler(reg, "/mission", srv.isRetracted);
    const payloads = [
      { title: "A", tag: "needs-you:a:1" },
      { title: "B", tag: "needs-you:b:1", close: ["needs-you:a:1"] },
      {},
    ];
    for (const [i, p] of payloads.entries()) {
      await push(p);
      expect(shown.length).toBe(i + 1);
    }
  });

  test("a show carries owed retractions and applies them first, closing only those", async () => {
    const { reg, open } = shade();
    const push = createPushHandler(reg, "/mission", server().isRetracted);
    await push({ title: "A", tag: "needs-you:a:1" });
    await push({ title: "C", tag: "needs-you:c:1" });
    await push({ title: "B", tag: "needs-you:b:1", close: ["needs-you:a:1"] });
    expect([...open.keys()].sort()).toEqual(["needs-you:b:1", "needs-you:c:1"]);
  });

  test("a delayed delivery of an episode the SERVER already retracted is shown silently and closed", async () => {
    const { reg, open, shown } = shade();
    const push = createPushHandler(
      reg,
      "/mission",
      server(["needs-you:a:1"]).isRetracted,
    );
    await push({ title: "A", tag: "needs-you:a:1" });
    expect(shown).toEqual([{ title: "A", tag: "needs-you:a:1", silent: true }]);
    expect(open.size).toBe(0);
  });

  test("a live episode is shown with sound and stays", async () => {
    const { reg, open, shown } = shade();
    const push = createPushHandler(reg, "/mission", server().isRetracted);
    await push({ title: "A", tag: "needs-you:a:1" });
    expect(shown[0].silent).toBe(false);
    expect(open.has("needs-you:a:1")).toBe(true);
  });

  test("an episode that ends WHILE its show is in flight is closed after the show (Hermes 5275)", async () => {
    const { reg, open, hold } = shade();
    const srv = server();
    const push = createPushHandler(reg, "/mission", srv.isRetracted);
    const release = hold(); // the platform show is slow
    const shown = push({ title: "A", tag: "needs-you:a:1" });
    await vi.waitFor(() => expect(srv.asked).toEqual(["needs-you:a:1"])); // pre-show: live
    // The episode ends and the app reconciles while the show is still in flight: nothing to close yet.
    srv.retracted.add("needs-you:a:1");
    expect(await reconcileDeviceNotifications(reg, ["needs-you:a:1"])).toBe(0);
    release();
    await shown;
    expect(open.has("needs-you:a:1")).toBe(false);
  });

  test("a check the server cannot answer fails OPEN — the notification is shown and kept", async () => {
    const { reg, open, shown } = shade();
    const push = createPushHandler(reg, "/mission", async () => {
      throw new Error("offline");
    });
    await push({ title: "A", tag: "needs-you:a:1" });
    expect(shown[0].silent).toBe(false);
    expect(open.has("needs-you:a:1")).toBe(true);
  });

  test("notifications under a REUSED tag are never asked about", async () => {
    const { reg } = shade();
    const srv = server(["/s/claude/x", "mission"]);
    const push = createPushHandler(reg, "/mission", srv.isRetracted);
    await push({ title: "M", url: "/s/claude/x" });
    await push({ title: "N" });
    expect(srv.asked).toEqual([]);
  });

  test("a failure applying retractions never stops the new notification", async () => {
    const { reg, shown } = shade();
    const failing: NotificationSurface = {
      showNotification: reg.showNotification,
      getNotifications: async () => {
        throw new Error("platform");
      },
    };
    const push = createPushHandler(failing, "/mission", server().isRetracted);
    await push({ title: "B", tag: "needs-you:b:1", close: ["needs-you:a:1"] });
    expect(shown.map((s) => s.tag)).toEqual(["needs-you:b:1"]);
  });

  test("pushes apply in arrival order even when a show is slow", async () => {
    const { reg, open, hold } = shade();
    const push = createPushHandler(reg, "/mission", server().isRetracted);
    const release = hold();
    const first = push({ title: "A", tag: "needs-you:a:1" });
    const second = push({
      title: "B",
      tag: "needs-you:b:1",
      close: ["needs-you:a:1"],
    });
    release();
    await Promise.all([first, second]);
    expect([...open.keys()]).toEqual(["needs-you:b:1"]);
  });
});

describe("mission episode notifications (#2154)", () => {
  const REFUSAL = "mission-ep:mission-refusal:apr1:0123456789abcdef01234567";
  const NEWER = "mission-ep:mission-refusal:apr1:fedcba9876543210fedcba98";
  const REWORK = "mission-ep:mission-rework:msn_1:o1.cv:1";

  test("the worker asks about them before and after the show", async () => {
    const { reg, open } = shade();
    const srv = server();
    const push = createPushHandler(reg, "/mission", srv.isRetracted);
    await push({ title: "R", url: "/mission", tag: REFUSAL });
    await push({ title: "Q", url: "/mission", tag: REWORK });
    expect(srv.asked).toEqual([REFUSAL, REFUSAL, REWORK, REWORK]);
    expect([...open.keys()]).toEqual([REFUSAL, REWORK]);
  });

  test("a delayed delivery the server already retracted is shown silently and closed", async () => {
    const { reg, open, shown } = shade();
    const push = createPushHandler(
      reg,
      "/mission",
      server([REFUSAL]).isRetracted,
    );
    await push({ title: "R", tag: REFUSAL });
    expect(shown).toEqual([{ title: "R", tag: REFUSAL, silent: true }]);
    expect(open.size).toBe(0);
  });

  test("a retraction that lands while the show is in flight closes it after the show", async () => {
    const { reg, open, hold } = shade();
    const srv = server();
    const push = createPushHandler(reg, "/mission", srv.isRetracted);
    const release = hold();
    const shown = push({ title: "R", tag: REFUSAL });
    await vi.waitFor(() => expect(srv.asked).toEqual([REFUSAL]));
    srv.retracted.add(REFUSAL);
    expect(await reconcileDeviceNotifications(reg, [REFUSAL])).toBe(0);
    release();
    await shown;
    expect(open.has(REFUSAL)).toBe(false);
  });

  test("reordered delivery: the close arrives before its notification, which then stays closed", async () => {
    const { reg, open } = shade();
    const srv = server([REFUSAL]);
    const push = createPushHandler(reg, "/mission", srv.isRetracted);
    await push({ title: "N", tag: NEWER, close: [REFUSAL] }); // the later push came first
    await push({ title: "R", tag: REFUSAL }); // the older, already retracted, arrives late
    expect([...open.keys()]).toEqual([NEWER]);
  });

  test("closing an older episode leaves the newer one open", async () => {
    const { reg, open } = shade();
    const push = createPushHandler(reg, "/mission", server().isRetracted);
    await push({ title: "R", tag: REFUSAL });
    await push({ title: "N", tag: NEWER, close: [REFUSAL] });
    expect([...open.keys()]).toEqual([NEWER]);
    expect(await reconcileDeviceNotifications(reg, [REFUSAL])).toBe(0);
    expect(open.has(NEWER)).toBe(true);
  });
});

describe("serverRetractions", () => {
  const reply = (body: unknown, ok = true) =>
    vi.fn(async () => ({ ok, json: async () => body }) as unknown as Response);

  test("answers from the bell's close_tags", async () => {
    const f = reply({ close_tags: ["needs-you:a:1"] });
    const ask = serverRetractions(f);
    expect(await ask("needs-you:a:1")).toBe(true);
    expect(await ask("needs-you:b:1")).toBe(false);
    expect(f).toHaveBeenCalledWith(
      "/api/pulse/notifications",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  test("an error, a missing list or a refused read is 'not retracted'", async () => {
    expect(await serverRetractions(reply({}, false))("t")).toBe(false);
    expect(await serverRetractions(reply({}))("t")).toBe(false);
    expect(await serverRetractions(reply({ close_tags: "t" }))("t")).toBe(
      false,
    );
  });

  test("a read slower than the budget is aborted", async () => {
    const ask = serverRetractions(
      (_i, init) =>
        new Promise((_r, reject) =>
          init?.signal?.addEventListener("abort", () =>
            reject(new Error("aborted")),
          ),
        ),
      5,
    );
    await expect(ask("t")).rejects.toThrow("aborted");
  });
});

describe("app-open reconcile", () => {
  test("closes EXACTLY the tags the server lists", async () => {
    const { reg, open } = shade();
    const push = createPushHandler(reg, "/mission", server().isRetracted);
    await push({ title: "A", tag: "needs-you:a:1" });
    await push({ title: "B", tag: "needs-you:b:1" });
    await push({ title: "M", url: "/s/claude/m" });
    const n = await reconcileDeviceNotifications(reg, [
      "needs-you:a:1",
      "/s/claude/gone",
    ]);
    expect(n).toBe(1);
    expect([...open.keys()].sort()).toEqual(["/s/claude/m", "needs-you:b:1"]);
  });

  test("an empty list closes nothing", async () => {
    const { reg, open } = shade();
    const push = createPushHandler(reg, "/mission", server().isRetracted);
    await push({ title: "A", tag: "needs-you:a:1" });
    expect(await reconcileDeviceNotifications(reg, [])).toBe(0);
    expect(open.size).toBe(1);
  });
});
