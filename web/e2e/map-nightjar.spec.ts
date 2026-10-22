/** The map in NIGHTJAR (#1573 Phase 9, docs/design.md v2 §8) in a real browser, on a POPULATED map:
 *  folder clusters nested three deep (so hierarchy edges exist, one of them under a working agent),
 *  every cluster expanded, two session windows open over it, the minimap shown.
 *
 *  What a jsdom test cannot prove, and #1573's Evidence asks for:
 *  - the idle cost stays inside the 2% budget in Full (measured, not inferred from "CSS on the
 *    compositor");
 *  - Off stops everything, Reduced runs no loop, a hidden tab pauses the ambient effects;
 *  - only a LIVE edge carries the data-flow; panning creates no NIGHTJAR motion and React Flow's
 *    viewport has no transition (pan and zoom stay its own);
 *  - the window invariants hold under the restyle: the layer is a sibling of React Flow, outside
 *    its transform; a window's chrome bar is its only bar; the file drawer overlays the body and
 *    never resizes it (docs/invariants/map-window-layer.md);
 *  - re-entry paints the retained list while it revalidates (#1007,
 *    docs/invariants/map-session-retention.md);
 *  - the shared React Flow theme's Controls are 44 px plates. */
import { expect, type Page, test } from "@playwright/test";
import { expectIdleBudget, storeMotionChoice } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";
import { mockRoster } from "./roster";

const now = Math.floor(Date.now() / 1000);
const row = (n: number, cwd: string, working: boolean) => ({
  id: `claude:s${n}`,
  engine: "claude",
  uuid: `s${n}`,
  short_uuid: `s${n}`,
  cwd,
  project: { kind: "project", id: "p1", name: "app", color: "#36d399" },
  last_mtime: now - n * 60,
  first_user_message: "",
  title: `Map session ${n}`,
  sticky: false,
  archived: false,
  ai_summary: `Summary ${n}`,
  working,
  intervention_required: n === 3,
  intervention_reason: n === 3 ? "Wants approval" : "",
});
// /home/u/app → /home/u/app/api (a working agent: its edge is LIVE) and /home/u/app/web (idle:
// its edge is not); /home/u/docs is a second root.
const SESSIONS = [
  row(1, "/home/u/app", true),
  row(2, "/home/u/app", false),
  row(3, "/home/u/app/api", true),
  row(4, "/home/u/app/web", false),
  row(5, "/home/u/docs", false),
];
const EXPANDED = [
  "/home/u/app",
  "/home/u/app/api",
  "/home/u/app/web",
  "/home/u/docs",
];

interface Log {
  resize: Record<string, number>;
}

async function mockMap(
  page: Page,
  opts: { holdSessions?: () => boolean } = {},
): Promise<Log> {
  const log: Log = { resize: {} };
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await page.route("**/api/**/draft", (r) =>
    r.fulfill({
      json: { id: "d", text: "", attachments: [], updated_at: null },
    }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({
      json: {
        projects: [
          { id: "p1", name: "app", color: "#36d399", archived: false },
        ],
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/files/capabilities", (r) =>
    r.fulfill({ json: { ok: true, reason: "" } }),
  );
  await page.route("**/api/files/list**", (r) =>
    r.fulfill({
      json: {
        path: "/home/u/app/api",
        parent: "/home/u/app",
        root: "/home/u",
        entries: [
          {
            name: "main.py",
            path: "/home/u/app/api/main.py",
            kind: "file",
            size: 20,
            mtime: now,
          },
        ],
        total: 1,
        complete: true,
        truncated: false,
      },
    }),
  );
  await page.route("**/api/sessions?**", async (r) => {
    // A held revalidation (the retention check) never answers while the hold is on.
    if (opts.holdSessions?.()) return;
    await r.fulfill({
      json: {
        sessions: SESSIONS,
        next_offset: null,
        total: SESSIONS.length,
        facets: { projects: [], engines: [] },
      },
    });
  });
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        onboarded: true,
        overview_expanded: EXPANDED,
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
        pulse: { configured: true },
      },
    }),
  );
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    const key = decodeURIComponent(
      new URL(ws.url()).pathname.replace("/ws/term/", ""),
    );
    log.resize[key] ??= 0;
    ws.onMessage((raw) => {
      if (typeof raw === "string" && raw.includes('"t":"r"'))
        log.resize[key] += 1;
    });
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    ws.send(
      Buffer.from(
        `\x1b[2J\x1b[Hhello from ${key}\r\n${"Ready terminal content\r\n".repeat(30)}`,
      ),
    );
  });
  await mockRoster(page);
  await page.addInitScript(() => {
    localStorage.setItem("tr-overview-groupby", "folder");
    if (sessionStorage.getItem("seeded")) return;
    sessionStorage.setItem("seeded", "1");
    localStorage.setItem(
      "tr-overview-workspace",
      JSON.stringify([
        {
          key: "claude:s1",
          engine: "claude",
          id: "s1",
          title: "Map session 1",
          x: 560,
          y: 40,
          w: 600,
          h: 340,
          z: 1,
        },
        {
          key: "claude:s3",
          engine: "claude",
          id: "s3",
          title: "Map session 3",
          x: 600,
          y: 400,
          w: 600,
          h: 340,
          z: 2,
        },
      ]),
    );
  });
  return log;
}

/** Open the populated map: clusters, edges, both windows and the minimap on screen. */
async function openMap(page: Page) {
  await page.goto("/overview");
  await expect(page.locator(".tr-overview .tr-ov-chip")).toHaveCount(
    SESSIONS.length,
  );
  await expect(page.locator("[data-session-window]")).toHaveCount(2);
  await expect(page.locator(".react-flow__minimap")).toBeVisible();
  await expect(page.locator(".react-flow__edge")).toHaveCount(2);
}

/** Running infinite CSS animations that belong to the map's OWN motion: the canvas, its
 *  decoration and minimap — not the app atmosphere, and not the session panes inside the windows
 *  (their LEDs and loaders are the pane's, PR 3). */
async function mapLoops(page: Page): Promise<number> {
  return page.evaluate(
    () =>
      document
        .getAnimations()
        .filter((a) => a instanceof CSSAnimation)
        .filter(
          (a) =>
            a.playState === "running" &&
            a.effect?.getComputedTiming().iterations === Infinity,
        )
        .filter((a) => {
          const t = (a.effect as KeyframeEffect).target as Element | null;
          return (
            !!t?.closest?.(".tr-overview") && !t.closest("[data-window-layer]")
          );
        }).length,
  );
}

/** The names of the map's own CSS animations (as `mapLoops`), sorted. */
async function mapAnimationNames(page: Page): Promise<string> {
  return page.evaluate(() =>
    document
      .getAnimations()
      .filter((a): a is CSSAnimation => a instanceof CSSAnimation)
      .filter((a) => {
        const t = (a.effect as KeyframeEffect).target as Element | null;
        return (
          !!t?.closest?.(".tr-overview") && !t.closest("[data-window-layer]")
        );
      })
      .map((a) => a.animationName)
      .sort()
      .join(","),
  );
}

test.describe("desktop: the populated map with windows", () => {
  test.beforeEach(async ({ page }, info) => {
    test.skip(
      info.project.name !== "desktop",
      "windows exist on a desktop map only (#208)",
    );
    await page.emulateMedia({ reducedMotion: "no-preference" });
  });

  test("Full: only the LIVE edge flows; the radar, the working LEDs and the focused tether run (#1573)", async ({
    page,
  }) => {
    await mockMap(page);
    await storeMotionChoice(page, "full");
    await openMap(page);
    const live = page.locator(".react-flow__edge.tr-ov-edge-live");
    await expect(live).toHaveCount(1);
    // The data-flow: one packet on the one live edge, and nothing on the idle one.
    const packets = page.locator(".tr-ov-packet");
    await expect(packets).toHaveCount(1);
    for (const p of await packets.all())
      expect(
        await p.evaluate((el) =>
          el.getAnimations().map((a) => (a as CSSAnimation).animationName),
        ),
      ).toEqual(["tr-ov-packet"]);
    // Every map animation is the compositor's: transform / opacity only, nothing that repaints.
    const props = await page.evaluate(() =>
      [
        ...new Set(
          document
            .getAnimations()
            .filter((a) => a instanceof CSSAnimation)
            .filter((a) => {
              const t = (a.effect as KeyframeEffect).target as Element | null;
              return (
                !!t?.closest?.(".tr-overview") &&
                !t.closest("[data-window-layer]")
              );
            })
            .flatMap((a) =>
              (
                (a.effect as KeyframeEffect).getKeyframes() as Record<
                  string,
                  unknown
                >[]
              ).flatMap(Object.keys),
            )
            .filter(
              (k) =>
                !["offset", "easing", "composite", "computedOffset"].includes(
                  k,
                ),
            ),
        ),
      ].sort(),
    );
    expect(
      props.every((p) => p === "transform" || p === "opacity"),
      props.join(","),
    ).toBe(true);
    expect(
      await mapLoops(page),
      "the map's ambient loops run in Full",
    ).toBeGreaterThan(3);
  });

  test("the populated map idles cheaply in Full (< 2% busy on an idle host; recalcs and layouts per second everywhere) (#1573)", async ({
    page,
    browserName,
  }, info) => {
    test.skip(browserName !== "chromium", "CDP metrics");
    await mockMap(page);
    await storeMotionChoice(page, "full");
    await openMap(page);
    await page.waitForTimeout(2000); // the restore, the first refits and the deploy-ins settle
    await expectIdleBudget(page, "populated map", info);
  });

  test("Off stops everything on the map (#1573)", async ({ page }) => {
    await mockMap(page);
    await storeMotionChoice(page, "off");
    await openMap(page);
    await page.waitForTimeout(600);
    expect(
      await page.evaluate(() => document.getAnimations().length),
      "Off: nothing animates",
    ).toBe(0);
  });

  test("Reduced runs no loop on the map (#1573)", async ({ page }) => {
    await mockMap(page);
    await storeMotionChoice(page, "reduced");
    await openMap(page);
    await expect(page.locator("html")).toHaveAttribute(
      "data-motion",
      "reduced",
    );
    await page.waitForTimeout(600);
    const infinite = await page.evaluate(
      () =>
        document
          .getAnimations()
          .filter((a) => a.effect?.getComputedTiming().iterations === Infinity)
          .length,
    );
    expect(infinite, "Reduced: no loop").toBe(0);
  });

  test("a hidden tab pauses the map's ambient effects; a visible one resumes them (#1573)", async ({
    page,
  }) => {
    await mockMap(page);
    await storeMotionChoice(page, "full");
    await openMap(page);
    expect(await mapLoops(page)).toBeGreaterThan(0);
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "hidden",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect(page.locator("html")).toHaveAttribute(
      "data-motion-paused",
      "hidden",
    );
    await expect
      .poll(() => mapLoops(page), { message: "every map loop is paused" })
      .toBe(0);
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => mapLoops(page)).toBeGreaterThan(0);
  });

  test("panning is React Flow's own: no transition on the viewport, and a pan starts no NIGHTJAR motion (#1573)", async ({
    page,
  }) => {
    await mockMap(page);
    await storeMotionChoice(page, "full");
    await openMap(page);
    const viewport = page.locator(".react-flow__viewport");
    expect(
      await viewport.evaluate((el) => getComputedStyle(el).transitionDuration),
    ).toMatch(/^0s(, 0s)*$/);
    const before = await mapAnimationNames(page);
    const t0 = await viewport.evaluate((el) => el.style.transform);
    // Drag a spot of EMPTY canvas: the first grid point where the pane itself is under the pointer
    // (not a cluster, a window, the toolbar or a panel).
    const spot = await page.evaluate(() => {
      const r = document
        .querySelector(".react-flow__pane")!
        .getBoundingClientRect();
      for (let y = r.top + 60; y < r.bottom - 60; y += 24)
        for (let x = r.left + 80; x < r.right - 200; x += 24) {
          const hit = document.elementFromPoint(x, y);
          if (hit?.classList.contains("react-flow__pane")) return { x, y };
        }
      return null;
    });
    expect(spot, "an empty spot of canvas").not.toBeNull();
    const { x, y } = spot!;
    await page.mouse.move(x, y);
    await page.mouse.down();
    for (let i = 1; i <= 10; i++) await page.mouse.move(x + i * 12, y - i * 6);
    await page.mouse.up();
    expect(
      await viewport.evaluate((el) => el.style.transform),
      "the pan moved the map",
    ).not.toBe(t0);
    expect(
      await mapAnimationNames(page),
      "the pan created no map animation",
    ).toBe(before);
  });

  test("the window invariants hold: a sibling of React Flow outside its transform, ONE bar, a drawer that overlays (#1573)", async ({
    page,
  }) => {
    const log = await mockMap(page);
    await storeMotionChoice(page, "full");
    await openMap(page);
    const layer = page.locator("[data-window-layer]");
    // A sibling of <ReactFlow>, never a node inside it, and no ancestor transforms it.
    const placement = await layer.evaluate((el) => {
      const transformed: string[] = [];
      for (let a = el.parentElement; a; a = a.parentElement) {
        const t = getComputedStyle(a).transform;
        if (t && t !== "none") transformed.push(a.className.toString());
      }
      return {
        insideFlow: !!el.closest(".react-flow"),
        siblingOfFlow: [...(el.parentElement?.children ?? [])].some((c) =>
          c.classList.contains("react-flow"),
        ),
        transformed,
      };
    });
    expect(placement).toEqual({
      insideFlow: false,
      siblingOfFlow: true,
      transformed: [],
    });

    const win = page.locator('[data-session-window="claude:s3"]');
    // ONE bar: the chrome, and no pane head inside the window (#1109).
    await expect(win.locator("[data-window-head]")).toHaveCount(1);
    await expect(win.locator("[data-panel-head]")).toHaveCount(0);

    // The file drawer OVERLAYS the body: the body keeps its box and the agent gets no resize frame.
    const bodyBox = () =>
      win.locator("[data-window-body]").evaluate((el) => {
        const r = el.getBoundingClientRect();
        return { w: Math.round(r.width), h: Math.round(r.height) };
      });
    await expect(win.locator(".xterm")).toBeVisible();
    await page.waitForTimeout(700); // the restore's own refit settles first
    const before = await bodyBox();
    const frames = log.resize["claude:s3"] ?? 0;
    // The Files chip, wherever the bar's fold put it: inline, or in the ONE ⋯ menu.
    const slot = win.locator("[data-window-actions-slot]");
    const chip = slot.locator("[data-head-action='files']");
    await expect(
      chip.or(win.locator("[data-window-menu]")).first(),
    ).toBeVisible();
    if (await chip.isVisible()) await chip.click();
    else {
      await win.locator("[data-window-menu]").click();
      await page
        .locator("[role='menu'][aria-label='Session actions']")
        .last()
        .getByRole("menuitem", { name: "Browse session files" })
        .click();
    }
    await expect(win.locator("[data-window-files-drawer]")).toBeVisible();
    await page.waitForTimeout(700); // past the refit debounce
    expect(await bodyBox()).toEqual(before);
    expect(
      log.resize["claude:s3"] ?? 0,
      "opening the drawer sent no resize",
    ).toBe(frames);
  });

  test("re-entry paints the retained map while it revalidates (#1007)", async ({
    page,
  }) => {
    let hold = false;
    await mockMap(page, { holdSessions: () => hold });
    await storeMotionChoice(page, "full");
    await openMap(page);
    hold = true; // the next read never answers
    await page
      .locator('[data-testid="section-nav"] >> text=Dashboard')
      .first()
      .click();
    await expect(page.locator(".tr-overview")).toHaveCount(0);
    await page.goBack();
    // Painted from the retained list at once — no blocking first-read state over it.
    await expect(page.locator(".tr-overview .tr-ov-chip")).toHaveCount(
      SESSIONS.length,
    );
    await expect(page.locator("[data-map-state='loading']")).toHaveCount(0);
  });

  test("the shared React Flow theme: Controls are 44 px plates; the minimap mask is a token (#1573)", async ({
    page,
  }) => {
    await mockMap(page);
    await storeMotionChoice(page, "off");
    await openMap(page);
    const buttons = page.locator(".react-flow__controls-button");
    expect(await buttons.count()).toBeGreaterThan(0);
    for (const b of await buttons.all()) {
      const box = (await b.boundingBox())!;
      expect(Math.round(box.width)).toBe(44);
      expect(Math.round(box.height)).toBe(44);
    }
    // The mask's COMPUTED fill is the theme's token (hud/reactFlow.css), never the old hard-coded
    // rgba(0,0,0,0.45) from the TSX: resolve the token's own expression in place and compare.
    const fills = await page.locator(".react-flow__minimap-mask").evaluate((el) => {
      const probe = document.createElement("span");
      probe.style.color = "var(--xy-minimap-mask-background-color)";
      el.closest(".react-flow")!.appendChild(probe);
      const want = getComputedStyle(probe).color;
      probe.remove();
      return { got: getComputedStyle(el).fill, want };
    });
    expect(fills.got).toBe(fills.want);
    expect(fills.got).not.toBe("rgba(0, 0, 0, 0.45)");
  });
});

test.describe("phone", () => {
  test("the toolbar's controls and the canvas Controls are 44 px touch targets (#1573)", async ({
    page,
  }, info) => {
    test.skip(info.project.name !== "mobile", "the touch floor is the phone's");
    await mockMap(page);
    await storeMotionChoice(page, "off");
    await page.goto("/overview");
    await expect(
      page.locator(".tr-overview .tr-ov-chip").first(),
    ).toBeAttached();
    const targets = page.locator(
      ".tr-ov-toolbar button, .tr-ov-toolbar input, .react-flow__controls-button",
    );
    expect(await targets.count()).toBeGreaterThan(5);
    for (const t of await targets.all()) {
      if (!(await t.isVisible())) continue;
      const box = (await t.boundingBox())!;
      expect(
        Math.round(box.height),
        await t.evaluate((el) => el.outerHTML.slice(0, 80)),
      ).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
  });
});
