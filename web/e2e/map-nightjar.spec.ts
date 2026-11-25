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
 *  - the strip's view controls (Fit, zoom) are plates on the touch floor (#1600: React Flow's
 *    own Controls stack is gone; the controls live in the header strip). */
import { expect, type Page, test } from "@playwright/test";
import { animationsSettled, expectIdleBudget, storeMotionChoice } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";
import { mockRoster } from "./roster";

const now = Math.floor(Date.now() / 1000);
const row = (n: number, cwd: string, working: boolean, running = working) => ({
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
  running,
  intervention_required: n === 3,
  intervention_reason: n === 3 ? "Wants approval" : "",
});
// /home/u/app → /home/u/app/api (a working agent: its edge is LIVE) and /home/u/app/web (idle:
// its edge is not); /home/u/docs is a second root.
const SESSIONS = [
  row(1, "/home/u/app", true),
  row(2, "/home/u/app", false),
  row(3, "/home/u/app/api", true),
  // Running but idle (#1600): it counts toward the strip's LIVE without lighting its link.
  row(4, "/home/u/app/web", false, true),
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

  test("the shared React Flow theme: the minimap mask is a token; the view controls live in the strip (#1573, #1600)", async ({
    page,
  }) => {
    await mockMap(page);
    await storeMotionChoice(page, "off");
    await openMap(page);
    // #1600: React Flow's Controls stack is replaced by Fit / zoom in / zoom out in the strip.
    await expect(page.locator(".react-flow__controls")).toHaveCount(0);
    for (const sel of ["[data-map-fit]", "[data-map-zoom-in]", "[data-map-zoom-out]"])
      await expect(page.locator(`[data-map-strip] ${sel}`)).toBeVisible();
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
  test("the strip's controls, view controls included, are touch targets (#1573, #1600)", async ({
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
      ".tr-ov-toolbar button, .tr-ov-toolbar input",
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

// ---- the v5 composition (#1600) ----------------------------------------------------------------

/** Every visible control in a bar sits on ONE row inside the bar, none overlapping the next, each
 *  with an accessible name; the text runs beside them are not clipped. */
async function expectOneRow(page: Page, bar: string, label: string) {
  const r = await page.locator(bar).first().evaluate((el) => {
    const b = el.getBoundingClientRect();
    const controls = [...el.querySelectorAll<HTMLElement>("button, input")]
      .filter((c) => c.getClientRects().length > 0 && getComputedStyle(c).visibility !== "hidden")
      .map((c) => {
        const r = c.getBoundingClientRect();
        const name = (
          c.getAttribute("aria-label") ||
          c.getAttribute("title") ||
          c.textContent ||
          ""
        ).trim();
        return { l: r.left, r: r.right, t: r.top, b: r.bottom, name, html: c.outerHTML.slice(0, 60) };
      })
      .sort((a, z) => a.l - z.l);
    return { bar: { t: b.top, b: b.bottom, h: b.height }, controls };
  });
  expect(r.controls.length, label).toBeGreaterThan(3);
  for (const c of r.controls) {
    expect(c.name, `${label}: ${c.html} has an accessible name`).not.toBe("");
    expect(c.t, `${label}: ${c.html} is inside the bar`).toBeGreaterThanOrEqual(r.bar.t - 0.5);
    expect(c.b, `${label}: ${c.html} is inside the bar`).toBeLessThanOrEqual(r.bar.b + 0.5);
  }
  for (let i = 1; i < r.controls.length; i++)
    expect(r.controls[i].l, `${label}: ${r.controls[i].html} overlaps the control before it`).toBeGreaterThanOrEqual(
      r.controls[i - 1].r - 0.5,
    );
  return r;
}

test.describe("the v5 composition (#1600)", () => {
  test.beforeEach(async ({ page }, info) => {
    test.skip(info.project.name !== "desktop", "the strip's one-row widths and the windows are desktop's");
    await storeMotionChoice(page, "off");
  });

  test("the strip: real counts, LIVE is running processes (not lit links), every control on ONE row at 1440, 1100 and 801 with windows, Reset and the New-project form", async ({
    page,
  }) => {
    await mockMap(page);
    // A pinned cluster, so Reset layout shows (any pin in this layout does).
    await page.addInitScript(() =>
      localStorage.setItem(
        "tr-overview-cluster-positions",
        JSON.stringify({ folder: { "/home/u/docs": { x: 40, y: 40 } } }),
      ),
    );
    await page.setViewportSize({ width: 1440, height: 900 });
    await openMap(page);
    const strip = page.locator("[data-map-strip]");
    // Four folder sectors, five units; LIVE = the three with a process running (s1, s3 working,
    // s4 running but idle) — while only the working agents light their sectors and links.
    await expect(strip.locator('[data-map-count="sectors"]')).toHaveText("4");
    await expect(strip.locator('[data-map-count="units"]')).toHaveText("5");
    await expect(strip.locator('[data-map-count="live"]')).toHaveText("3");
    const web = page.locator(".tr-ov-group", { has: page.locator(".tr-ov-path", { hasText: /web$/ }) });
    await expect(web).toHaveCount(1);
    await expect(web).not.toHaveAttribute("data-sector-live", "");
    await expect(page.locator(".tr-ov-group[data-sector-live]")).toHaveCount(2);
    await expect(page.locator("[data-reset-layout]")).toBeAttached();

    for (const width of [1440, 1100, 801]) {
      await page.setViewportSize({ width, height: 900 });
      await animationsSettled(page);
      const r = await expectOneRow(page, "[data-map-strip]", `strip at ${width}`);
      expect(r.bar.h, `the strip at ${width} is one row`).toBeLessThan(64);
      // The callsign never clips: the ladder drops the counts or the name instead.
      const clipped = await strip
        .locator(".tr-ov-strip-title")
        .evaluate((el) => el.scrollWidth > el.clientWidth + 1);
      expect(clipped, `the strip title at ${width}`).toBe(false);
      // Labels collapse to icons but keep their accessible names.
      await expect(strip.getByRole("button", { name: /Close every open session window|Close all/ })).toHaveCount(1);
    }

    // The New-project form editing at 801: still one row, and every control keyboard-reachable
    // (the strip scrolls a focused control into view rather than wrapping).
    await strip.getByRole("button", { name: /New project|Create a project entity/ }).click();
    await expect(strip.getByRole("textbox", { name: "Project name" })).toBeFocused();
    await expectOneRow(page, "[data-map-strip]", "strip at 801, New project editing");
    const last = strip.locator("[data-map-zoom-out]");
    for (let i = 0; i < 30 && !(await last.evaluate((el) => el === document.activeElement)); i++)
      await page.keyboard.press("Tab");
    await expect(last).toBeFocused();
    const inView = await last.evaluate((el) => {
      const b = el.getBoundingClientRect();
      const s = el.closest("[data-map-strip]")!.getBoundingClientRect();
      return b.left >= s.left - 0.5 && b.right <= s.right + 0.5;
    });
    expect(inView, "a focused control is scrolled into the strip's view").toBe(true);
  });

  test("sectors and units read like the board: the callsign inside the armour panel, status words, the awaiting edge; legend and framed minimap", async ({
    page,
  }) => {
    await mockMap(page);
    await openMap(page);
    for (const g of await page.locator(".tr-ov-group").all()) {
      const inside = await g.evaluate((el) => {
        const b = el.getBoundingClientRect();
        const n = el.querySelector(".tr-ov-notch")!.getBoundingClientRect();
        const k = el.querySelector(".tr-ov-kind")!.getBoundingClientRect();
        const clip = getComputedStyle(el).clipPath;
        return {
          notch: n.left >= b.left && n.right <= b.right && n.top >= b.top && n.bottom <= b.bottom,
          kind: k.left >= b.left && k.top >= b.top,
          clip,
        };
      });
      expect(inside.notch, "the hazard notch sits inside the sector's frame").toBe(true);
      expect(inside.kind).toBe(true);
      expect(inside.clip, "the sector is a chamfered armour panel").toContain("polygon");
    }
    const chip = (n: number) => page.locator(`.tr-ov-chip[data-unit-state]`, { hasText: `Map session ${n}` });
    await expect(chip(1)).toHaveAttribute("data-unit-state", "engaged");
    await expect(chip(1).locator(".tr-ov-unit-state")).toHaveText("Engaged");
    await expect(chip(3)).toHaveAttribute("data-unit-state", "awaiting");
    await expect(chip(3).locator(".tr-ov-unit-state")).toHaveText("Awaiting orders");
    // The card's callsign is ENGINE-ID (#1600): two units of the same engine never share one.
    const callsigns = await page.locator(".tr-ov-chip .tr-ov-callsign").allTextContents();
    expect(callsigns.length).toBeGreaterThan(1);
    expect(callsigns.every((c) => /^\S+-s\d+$/.test(c)), callsigns.join(", ")).toBe(true);
    expect(new Set(callsigns).size, "each unit's callsign is its own").toBe(callsigns.length);
    await expect(chip(4)).toHaveAttribute("data-unit-state", "standby");
    await expect(chip(5)).toHaveAttribute("data-unit-state", "offline");
    const edge = (n: number) => chip(n).evaluate((el) => getComputedStyle(el).borderTopColor);
    expect(await edge(3), "the awaiting card sits on the amber edge").not.toBe(await edge(5));
    // The board's FAILED has no source: OFFLINE replaces it, and no LIVE-ONLY control exists.
    await expect(page.locator("[data-map-legend] li")).toHaveText([
      "Engaged",
      "Awaiting orders",
      "Standby",
      "Offline",
    ]);
    await expect(page.getByText(/live only/i)).toHaveCount(0);
    const legend = (await page.locator("[data-map-legend]").boundingBox())!;
    const mini = (await page.locator(".react-flow__minimap").boundingBox())!;
    const map = (await page.locator("[data-overview-map]").boundingBox())!;
    expect(legend.x - map.x, "legend bottom-left").toBeLessThan(40);
    expect(map.x + map.width - (mini.x + mini.width), "minimap bottom-right").toBeLessThan(40);
    expect(
      await page.locator(".react-flow__minimap").evaluate((el) => getComputedStyle(el).borderTopWidth),
      "the minimap is framed",
    ).toBe("2px");
  });

  test("an empty project still counts as a sector; a map with no nodes and an open window reads 0 // 0 // 0", async ({
    page,
  }) => {
    let empty = false;
    await mockMap(page);
    await page.route("**/api/sessions?**", (r) =>
      empty
        ? r.fulfill({
            json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
          })
        : r.fallback(),
    );
    await openMap(page);
    // Every session gone (the windows stay, #208): re-entry revalidates to an empty list.
    empty = true;
    await page.locator('[data-testid="section-nav"] >> text=Dashboard').first().click();
    await expect(page.locator(".tr-overview")).toHaveCount(0);
    await page.goBack();
    const strip = page.locator("[data-map-strip]");
    await expect(page.locator(".tr-overview .tr-ov-chip")).toHaveCount(0);
    await expect(page.locator("[data-session-window]")).toHaveCount(2);
    // Folder grouping with no sessions: no nodes at all — the empty state, the windows live.
    await expect(strip.locator('[data-map-count="sectors"]')).toHaveText("0");
    await expect(strip.locator('[data-map-count="units"]')).toHaveText("0");
    await expect(strip.locator('[data-map-count="live"]')).toHaveText("0");
    // Grouped by project, the empty project (#447) is a sector with no units.
    await strip.getByRole("radio", { name: "Group by projects" }).click();
    await expect(page.locator(".tr-ov-group")).toHaveCount(1);
    await expect(strip.locator('[data-map-count="sectors"]')).toHaveText("1");
    await expect(strip.locator('[data-map-count="units"]')).toHaveText("0");
  });
});

const API_ROW = {
  ...row(6, "/home/u/docs", false, true),
  id: "claude-api:a6",
  engine: "claude-api",
  uuid: "a6",
  short_uuid: "a6",
  title: "Map API session",
  model_configured: "sonnet-test",
};

/** One terminal window and one API window, side by side; the API session's own reads mocked. */
async function mockWindowPair(page: Page, terminalW = 600, apiRow: typeof API_ROW = API_ROW) {
  await mockMap(page);
  await page.route("**/api/sessions?**", (r) =>
    r.fulfill({
      json: {
        sessions: [...SESSIONS, apiRow],
        next_offset: null,
        total: SESSIONS.length + 1,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route(/\/api\/sessions\/claude-api(?::|%3A)a6$/, (r) => r.fulfill({ json: apiRow }));
  await page.route(/\/api\/structured\/sessions\/claude-api(?::|%3A)a6(\/[a-z]+)?(\?.*)?$/, (r) => {
    const snap = {
      session_key: "claude-api:a6",
      revision: 1,
      event_cursor: 1,
      cwd: "/home/u/docs",
      state: "idle",
      active_turn: null,
      model_requested: null,
      model_effective: null,
      turns: [],
      omitted_turns: 0,
      pending_requests: [],
      native: { native_id: "n6", worker: "w1", background_active: false },
      read_only: null,
    };
    if (r.request().url().includes("/events"))
      return r.fulfill({
        json: { session_key: snap.session_key, revision: 1, next_cursor: 1, events: [] },
      });
    return r.fulfill({ json: snap });
  });
  await page.addInitScript((w) => {
    localStorage.setItem(
      "tr-overview-workspace",
      JSON.stringify([
        { key: "claude:s1", engine: "claude", id: "s1", title: "Map session 1", x: 40, y: 60, w, h: 360, z: 1 },
        {
          key: "claude-api:a6",
          engine: "claude-api",
          id: "a6",
          title: "Map API session",
          x: 60 + w,
          y: 60,
          w: 600,
          h: 360,
          z: 2,
        },
      ]),
    );
  }, terminalW);
}

/** Open the map with the pair side by side: the session list collapsed, so both fit. */
async function openPair(page: Page) {
  await page.goto("/overview");
  await page.getByRole("button", { name: "Collapse session list" }).first().click();
  await expect(page.locator('[data-session-window="claude:s1"]')).toBeVisible();
  await expect(page.locator('[data-session-window="claude-api:a6"]')).toBeVisible();
  await animationsSettled(page);
}

test.describe("the v5 windows (#1600): terminal and API alike", () => {
  test.beforeEach(async ({ page }, info) => {
    test.skip(info.project.name !== "desktop", "windows exist on a desktop map only (#208)");
    await storeMotionChoice(page, "off");
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  test("both windows are the same armour panel: chamfer with the edge along the cut, rail, ticks, callsign bar, footer — and nothing over the work surface", async ({
    page,
  }) => {
    await mockWindowPair(page);
    await openPair(page);
    const term = page.locator('[data-session-window="claude:s1"]');
    const api = page.locator('[data-session-window="claude-api:a6"]');
    await expect(term).toBeVisible();
    await expect(api).toBeVisible();
    // Focus the API window (a press on its bar), so the pair shows focused beside unfocused.
    await api.locator("[data-window-head]").click({ position: { x: 200, y: 15 } });
    await expect(api).toHaveAttribute("data-focused", "true");
    await expect(term.locator("[data-window-foot]")).toHaveAttribute("data-window-foot", "terminal");
    await expect(api.locator("[data-window-foot]")).toHaveAttribute("data-window-foot", "api");
    await animationsSettled(page);

    const facts = async (win: typeof term) =>
      win.evaluate((el) => {
        const frame = el.querySelector<HTMLElement>("[data-window-frame]")!;
        const r = el.getBoundingClientRect();
        const body = el.querySelector("[data-window-body]")!.getBoundingClientRect();
        const rail = el.querySelector("[data-window-rail]")!.getBoundingClientRect();
        const cut = parseFloat(getComputedStyle(frame).clipPath.match(/polygon\(([\d.]+)px/)![1]);
        // Is the work surface the top element at its four inner corners? (Nothing decorative
        // drawn over it.)
        const pts = [
          [body.left + 2, body.top + 2],
          [body.right - 2, body.top + 2],
          [body.left + 2, body.bottom - 2],
          [body.right - 2, body.bottom - 2],
        ];
        const clean = pts.every(([x, y]) => {
          const hit = document.elementFromPoint(x, y);
          return !!hit && !!hit.closest("[data-window-body]");
        });
        return {
          cut,
          edge: getComputedStyle(frame).backgroundColor,
          ticks: el.querySelectorAll("[data-window-tick]").length,
          railTop: rail.top - r.top,
          railH: rail.height,
          bodyTop: body.top - r.top,
          bodyBottom: r.bottom - body.bottom,
          clean,
          callsign: el.querySelector("[data-window-callsign]")?.textContent ?? "",
          headH: el.querySelector("[data-window-head]")!.getBoundingClientRect().height,
        };
      });
    const t = await facts(term);
    const a = await facts(api);
    for (const [name, f] of [
      ["terminal", t],
      ["api", a],
    ] as const) {
      expect(f.cut, `${name}: the cut reads at window size`).toBeGreaterThanOrEqual(18);
      expect(f.ticks, `${name}: corner ticks on the uncut corners`).toBe(2);
      expect(f.railTop, `${name}: the rail sits on the top edge`).toBe(0);
      expect(f.railH).toBe(2);
      expect(f.headH, `${name}: the chrome bar is CHROME_H`).toBe(30);
      // The cuts reach only the bar and the footer — never the work surface.
      expect(f.bodyTop, `${name}: the body starts below the cut`).toBeGreaterThanOrEqual(f.cut);
      expect(f.bodyBottom, `${name}: the body ends above the cut`).toBeGreaterThanOrEqual(f.cut);
      expect(f.clean, `${name}: nothing is drawn over the work surface`).toBe(true);
    }
    // Same frame for both hosts; the focused one (the API window, z 2) on the full --hud edge.
    expect(t.cut).toBe(a.cut);
    expect(t.edge, "the unfocused edge is dimmer than the focused one").not.toBe(a.edge);
    // The callsign is ENGINE-ID for both hosts.
    expect(t.callsign).toMatch(/-s1$/);
    expect(a.callsign).toMatch(/-a6$/);

    // Footers: each reads its own facts — a terminal its grid, an API session its model.
    await expect(term.locator("[data-window-grid]")).toHaveText(/^\d+×\d+$/);
    await expect(api.locator("[data-window-grid]")).toHaveCount(0);
    await expect(api.locator("[data-window-model]")).toHaveText("sonnet-test");
    await expect(term.locator("[data-window-foot]")).toContainText(/Link\s*\/\/\s*(Live|Linking)/i);

    // The tether meets each window at a square connector node.
    await expect(page.locator("[data-tether] rect")).toHaveCount(2);
  });

  test("an API window's footer names the model the session RAN, not its startup report (#1511 resolution)", async ({
    page,
  }) => {
    // The client reported Haiku at start, a turn actually ran Opus: the observation wins, exactly
    // as the session pane and the recap resolve it.
    await mockWindowPair(page, 600, {
      ...API_ROW,
      model_configured: "haiku-start",
      model_effective: "opus-ran",
      model_requested: "sonnet-asked",
    } as typeof API_ROW);
    await openPair(page);
    const api = page.locator('[data-session-window="claude-api:a6"]');
    await expect(api.locator("[data-window-model]")).toHaveText("opus-ran");
  });

  test("a narrow window keeps the compact header: the controls as small icon plates on one row, named, nothing overlapping or wrapping — terminal and API", async ({
    page,
  }) => {
    await mockWindowPair(page, 560);
    // The API window narrowed to the minimum too.
    await page.addInitScript(() => {
      const raw = JSON.parse(localStorage.getItem("tr-overview-workspace")!);
      raw[1].w = 560;
      raw[1].x = 640;
      localStorage.setItem("tr-overview-workspace", JSON.stringify(raw));
    });
    await openPair(page);
    for (const key of ["claude:s1", "claude-api:a6"]) {
      const win = page.locator(`[data-session-window="${key}"]`);
      await expect(win).toBeVisible();
      await animationsSettled(page);
      const head = `[data-session-window="${key}"] [data-window-head]`;
      await expectOneRow(page, head, `${key} header at 560`);
      // The window's own controls are compact plates: small squares, chamfer painted (nj-plate).
      for (const name of ["Session actions", "Minimize window", "Open full screen", "Close window"]) {
        const b = win.getByRole("button", { name, exact: true });
        if (!(await b.count())) continue;
        const box = (await b.boundingBox())!;
        expect(box.width, `${key}: ${name} is a compact plate`).toBeLessThanOrEqual(26);
        expect(await b.evaluate((el) => el.classList.contains("nj-plate"))).toBe(true);
      }
      // The bar is one row: nothing in it is taller than the bar.
      const wrapped = await page.locator(head).evaluate((el) => el.scrollHeight > el.clientHeight + 1);
      expect(wrapped, `${key}: the header never wraps`).toBe(false);
    }
  });

  test("the bottom-right cut IS the resize grip: a press inside the cut corner resizes; a press in the top-left cut reaches the map", async ({
    page,
  }) => {
    await mockWindowPair(page);
    await openPair(page);
    const term = page.locator('[data-session-window="claude:s1"]');
    await expect(term).toBeVisible();
    await animationsSettled(page);
    const before = (await term.boundingBox())!;
    // 3 px in from the corner: outside the clipped frame, inside the grip's square.
    await page.mouse.move(before.x + before.width - 3, before.y + before.height - 3);
    await page.mouse.down();
    await page.mouse.move(before.x + before.width - 33, before.y + before.height - 23, { steps: 5 });
    await page.mouse.up();
    const after = (await term.boundingBox())!;
    expect(Math.round(before.width - after.width)).toBe(30);
    expect(Math.round(before.height - after.height)).toBe(20);
    // The top-left cut is not the window's: the press lands on whatever lies under it.
    const hit = await page.evaluate(
      ([x, y]) => !!document.elementFromPoint(x, y)?.closest("[data-session-window]"),
      [after.x + 3, after.y + 3],
    );
    expect(hit).toBe(false);
  });

  test("a minimised window parks as a chamfered tray plate", async ({ page }) => {
    await mockWindowPair(page);
    await openPair(page);
    const api = page.locator('[data-session-window="claude-api:a6"]');
    await expect(api).toBeVisible();
    await api.getByRole("button", { name: "Minimize window", exact: true }).click();
    const chip = page.locator('[data-window-tray-chip="claude-api:a6"]');
    await expect(chip).toBeVisible();
    expect(await chip.evaluate((el) => el.classList.contains("nj-plate"))).toBe(true);
    expect(
      await chip.evaluate((el) => getComputedStyle(el, "::before").clipPath),
    ).toContain("polygon");
  });
});
