import { expect, test, type Page } from "@playwright/test";
import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";
import { mockRoster } from "./roster";

/** THE TOP BAR'S nav, not "a link called Missions" (#1058).
 *
 *  The drawer renders the same five sections with the same names, so on a phone with the drawer
 *  open an unscoped `getByRole` resolves to two elements and Playwright's strict mode refuses.
 *  Scoping says which surface the assertion is about, which these tests always knew and used to
 *  get for free from the drawer copy not existing. */
const barLink = (page: Page, name: string) =>
  page
    .locator(".hud-topbar")
    .getByRole("navigation", { name: "Main sections" })
    .getByRole("link", { name, exact: true });

/** Select a mission from the rail (#948 P3). Nothing is auto-selected any more — `/mission` opens on
 *  the new-mission page — so a test about a selected mission picks one itself, through the shell's
 *  drawer on a phone. Without a title it takes the first row. */
async function selectMission(page: Page, title?: string) {
  await openMissionRail(page);
  const rows = page.getByTestId("rail-mission");
  await (title ? rows.filter({ hasText: title }) : rows).first().click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
}

export async function setupSections(page: Page) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        total: 0,
        next_offset: null,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: {
        projects: [
          { id: "p1", name: "BattleLab", folders: ["/repo"] },
          { id: "p2", name: "Infra", folders: ["/infra"] },
        ],
      },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await mockMissions(page, {
    missions: (q) => {
      const rows = [
        missionRow({ id: "msn_1", title: "Mission layout", project_id: "p1" }),
        missionRow({ id: "msn_2", title: "Infra checks", project_id: "p2" }),
      ].filter(
        (m) =>
          (!q.get("q") ||
            m.title.toLowerCase().includes(q.get("q")!.toLowerCase())) &&
          (!q.get("project") || m.project_id === q.get("project")),
      );
      return {
        ...missionList(rows),
        facets: { projects: ["p1", "p2"], states: ["running"] },
      };
    },
    mission: {
      ...MISSION,
      title: "Mission layout",
      project_id: "p1",
      events: Array.from({ length: 40 }, (_, i) => ({
        seq: i + 1,
        kind: "operator_msg",
        text: `Long history ${i}: ${"Keep this reachable. ".repeat(10)}`,
        ts: 1,
      })),
      events_next_seq: null,
    },
  });
}

test("section buttons share the brand row and keep their own declared typography at every width (#946, #967)", async ({
  page,
}) => {
  // Ten viewport widths, each a resize + relayout + a bounding-box read per section: ~15 s on an
  // idle host, and past the default 30 s budget on the shared runner under load — where it failed
  // on every PR's run as a TIMEOUT, never an assertion. The budget is the sweep's, like the
  // short-screen test's below; the assertions are unchanged.
  test.setTimeout(120_000);
  await setupSections(page);
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await selectMission(page, "Mission layout");
  await expect(page.getByTestId("console-title")).toHaveText("Mission layout");
  for (const width of [320, 360, 375, 412, 640, 641, 800, 801, 1100, 1440]) {
    await page.setViewportSize({ width, height: 740 });
    const header = page.locator(".hud-topbar");
    const brand = page.locator(".hud-brand");
    const nav = page.locator(".hud-topbar").getByRole("navigation", { name: "Main sections" });
    const hb = (await header.boundingBox())!;
    const bb = (await brand.boundingBox())!;
    expect(hb.height, `header at ${width}`).toBeLessThanOrEqual(52);
    // THE NAV'S OWN VALUES (#967). They were pinned to the mission Send's mono type (#944/#946); that
    // Send is now the session pane's Send, so the look is asserted as the values App.css declares:
    // --font-mono at 0.68rem and weight 400, 0.1em tracking, uppercase, 44px. Drift in the nav, or in
    // the tokens behind it, fails here.
    const declared = await page.evaluate(() => {
      const probe = document.createElement("span");
      probe.style.fontFamily = "var(--font-mono)";
      document.body.append(probe);
      const family = getComputedStyle(probe).fontFamily;
      probe.remove();
      const size = parseFloat(getComputedStyle(document.documentElement).fontSize) * 0.68;
      return { family, size, spacing: size * 0.1 };
    });
    // EVERY SECTION (#1058), in the #1069 order: Ask leads, and the map lives in the Sessions
    // menu rather than the row. Asserting a subset would let the rest regress.
    const chevron = nav.getByRole("button", { name: "Sessions menu" });
    const cb = (await chevron.boundingBox())!;
    expect(cb.height, `chevron height at ${width}`).toBe(44);
    expect(Math.abs(cb.y + cb.height / 2 - (bb.y + bb.height / 2)), `chevron row at ${width}`).toBeLessThan(2);
    await chevron.click({ trial: true });
    for (const name of ["Ask", "Sessions", "Missions", "Templates"]) {
      const link = nav.getByRole("link", { name, exact: true });
      const b = (await link.boundingBox())!;
      expect(
        Math.abs(b.y + b.height / 2 - (bb.y + bb.height / 2)),
        `${name} row at ${width}`,
      ).toBeLessThan(2);
      expect(b.x).toBeGreaterThanOrEqual(bb.x + bb.width);
      expect(b.x + b.width).toBeLessThanOrEqual(width);
      expect(b.height).toBeGreaterThanOrEqual(44);
      expect(b.height).toBeLessThanOrEqual(44);
      const look = await link.evaluate((el) => {
        const s = getComputedStyle(el);
        return {
          family: s.fontFamily,
          size: parseFloat(s.fontSize),
          weight: s.fontWeight,
          spacing: parseFloat(s.letterSpacing),
          minHeight: s.minHeight,
          transform: s.textTransform,
        };
      });
      expect(look.family, `${name} family at ${width}`).toBe(declared.family);
      expect(Math.abs(look.size - declared.size), `${name} size at ${width}`).toBeLessThan(0.01);
      expect(Math.abs(look.spacing - declared.spacing), `${name} tracking at ${width}`).toBeLessThan(0.01);
      expect([look.weight, look.minHeight, look.transform]).toEqual(["400", "44px", "uppercase"]);
      // NOT OVERLAPPED BY THE CORNER (#1058). The nav is allowed to shrink, so a row that does not
      // fit overflows its own box rather than the document — `scrollWidth` stays clean while the
      // last entry slides UNDER the notification bell and stops being clickable. Measured against
      // the corner cluster's left edge, which is the thing it collides with.
      const cluster = (await page
        .locator(".hud-topbar .hud-topbar-actions")
        .boundingBox())!;
      expect(b.x + b.width, `${name} clear of the corner at ${width}`).toBeLessThanOrEqual(
        cluster.x,
      );
      await link.click({ trial: true });
    }
    await page.locator(".hud-topbar > .navToggle").click({ trial: true });
    // EVERY kept control, not the first one (#1058 added the operator tile beside the bell). Both
    // must stay reachable at every width — that is the whole point of `data-topbar-keep`.
    const kept = page.locator(".hud-topbar [data-topbar-keep] button");
    expect(await kept.count(), `kept controls at ${width}`).toBe(2);
    for (let i = 0; i < 2; i++) {
      const b = (await kept.nth(i).boundingBox())!;
      expect(b.height, `kept ${i} height at ${width}`).toBeGreaterThanOrEqual(30);
      expect(b.x + b.width, `kept ${i} right edge at ${width}`).toBeLessThanOrEqual(width);
      await kept.nth(i).click({ trial: true });
    }
    // The operator tile is LAST in the corner — the user asked for the bell and the gear beside
    // it, and "beside" is an order, not a set.
    const bell = (await kept.nth(0).boundingBox())!;
    const tile = (await kept.nth(1).boundingBox())!;
    expect(tile.x, `tile after bell at ${width}`).toBeGreaterThan(bell.x);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width);
  }
});

test("ordinary buttons and section links glitch without losing hit areas (#946)", async ({
  page,
}) => {
  await setupSections(page);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await selectMission(page, "Mission layout");
  await expect(page.getByTestId("console-title")).toHaveText("Mission layout");
  await page.getByTestId("composer-input").fill("Keep this draft");
  const controls = [
    page.locator(".hud-topbar > .navToggle"),
    page.getByRole("button", { name: "Show full mission title" }),
    page.getByTestId("composer-send"),
    barLink(page, "Sessions"),
    // The split control's other half (#1069): both halves keep their whole hit area, including
    // the seam between them.
    page
      .locator(".hud-topbar")
      .getByRole("button", { name: "Sessions menu" }),
    barLink(page, "Missions"),
  ];
  for (const control of controls) {
    const result = await control.evaluate((el) => {
      const before = el.getBoundingClientRect();
      el.classList.add("glitching");
      const animation = el
        .getAnimations()
        .find((a) => (a as CSSAnimation).animationName === "hud-btn-glitch");
      if (!animation)
        return { animated: false, stable: false, hittable: false };
      animation.pause();
      let stable = true;
      let hittable = true;
      for (const time of [0, 75, 145, 215, 275]) {
        animation.currentTime = time;
        const rect = el.getBoundingClientRect();
        stable &&=
          rect.x === before.x &&
          rect.y === before.y &&
          rect.width === before.width &&
          rect.height === before.height;
        for (const x of [
          rect.left + 0.5,
          rect.left + rect.width / 2,
          rect.right - 0.5,
        ]) {
          for (const y of [
            rect.top + 0.5,
            rect.top + rect.height / 2,
            rect.bottom - 0.5,
          ]) {
            const hit = document.elementFromPoint(x, y);
            hittable &&= hit === el || el.contains(hit);
          }
        }
      }
      el.classList.remove("glitching");
      return { animated: true, stable, hittable };
    });
    expect(result).toEqual({ animated: true, stable: true, hittable: true });
    await control.click({ trial: true });
  }
  await page.emulateMedia({ reducedMotion: "reduce" });
  for (const control of controls) {
    expect(
      await control.evaluate((el) => {
        el.classList.add("glitching");
        const name = getComputedStyle(el).animationName;
        el.classList.remove("glitching");
        return name;
      }),
    ).toBe("none");
  }
  await barLink(page, "Sessions").click();
  await expect(
    barLink(page, "Sessions"),
  ).toHaveAttribute("aria-current", "page");
  await barLink(page, "Missions").click();
  await expect(
    barLink(page, "Missions"),
  ).toHaveAttribute("aria-current", "page");
});

test("ambient and press feedback reach ordinary controls and respect reduced motion (#946)", async ({
  page,
}) => {
  await page.clock.install();
  await page.addInitScript(() => {
    Math.random = () => 0;
  });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await setupSections(page);
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await selectMission(page, "Mission layout");
  await expect(page.getByTestId("console-title")).toHaveText("Mission layout");
  const toggle = page.locator(".hud-topbar > .navToggle");
  await page.clock.fastForward(7001);
  await expect(toggle).toHaveClass(/glitching/);
  await page.clock.fastForward(301);
  await expect(toggle).not.toHaveClass(/glitching/);

  const missions = barLink(page, "Missions");
  // The next ambient selection must skip a disabled ordinary button in the real DOM. With
  // `Math.random` pinned to 0 it takes the first candidate after the toggle — the first section in
  // the row, which is Ask since #1069.
  await toggle.evaluate((el) => ((el as HTMLButtonElement).disabled = true));
  await page.clock.fastForward(7001);
  await expect(toggle).not.toHaveClass(/glitching/);
  await expect(
    barLink(page, "Ask"),
  ).toHaveClass(/glitching/);
  await page.clock.fastForward(301);
  await toggle.evaluate((el) => ((el as HTMLButtonElement).disabled = false));
  // A modal's inert background cannot be an ambient candidate.
  await page
    .locator(".hud-topbar")
    .evaluate((el) => ((el as HTMLElement).inert = true));
  await page.clock.fastForward(7001);
  await expect(page.locator(".hud-topbar .glitching")).toHaveCount(0);
  await page
    .locator(".hud-topbar")
    .evaluate((el) => ((el as HTMLElement).inert = false));
  await page.clock.fastForward(301);
  const box = (await missions.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  expect(
    await missions.evaluate((el) => getComputedStyle(el).animationName),
  ).toBe("hud-btn-glitch");
  await page.mouse.up();
  await expect(missions).toHaveAttribute("aria-current", "page");

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.clock.fastForward(7001);
  await expect(page.locator(".glitching")).toHaveCount(0);
  await page.mouse.down();
  expect(
    await missions.evaluate((el) => getComputedStyle(el).animationName),
  ).toBe("none");
  await page.mouse.up();
});

test("compact header keeps drawer and notification anchors reachable (#946)", async ({
  page,
}) => {
  await setupSections(page);
  for (const width of [320, 800, 801]) {
    await page.setViewportSize({ width, height: 740 });
    await mockRoster(page); // the manifest-generated roster (#853 P4)
    await page.goto("/mission");
    await selectMission(page, "Mission layout");
    await expect(page.getByTestId("console-title")).toHaveText(
      "Mission layout",
    );
    if (width <= 800) {
      await page.locator(".hud-topbar > .navToggle").click();
      const drawer = page.locator(".sidebar");
      await expect(drawer).toHaveAttribute("role", "dialog");
      await expect.poll(async () => (await drawer.boundingBox())!.x).toBe(0);
      const box = (await drawer.boundingBox())!;
      expect(box.y).toBe(52);
      expect(box.y + box.height).toBe(740);
      await page.keyboard.press("Escape");
      await expect(drawer).not.toHaveAttribute("role", "dialog");
    }
    const bell = page.getByRole("button", {
      name: "Notifications",
      exact: true,
    });
    await bell.click();
    const panel = page.getByRole("dialog", {
      name: "Notifications",
      exact: true,
    });
    await expect(panel).toBeVisible();
    await panel.evaluate(async (el) => {
      await Promise.all(
        el.getAnimations().map((a) => a.finished.catch(() => {})),
      );
    });
    const before = (await panel.boundingBox())!;
    await bell.evaluate((el) => {
      el.classList.add("glitching");
      const animation = el.getAnimations()[0];
      animation?.pause();
      if (animation) animation.currentTime = 75;
    });
    expect(await panel.boundingBox()).toEqual(before);
    expect(before.x).toBeGreaterThanOrEqual(0);
    expect(before.x + before.width).toBeLessThanOrEqual(width);
    await page.keyboard.press("Escape");
    await expect(panel).toBeHidden();
    await expect(bell).toBeFocused();
  }
});

test("sections restore mission search in both navigation directions", async ({
  page,
  isMobile,
}) => {
  await setupSections(page);
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await expect(
    barLink(page, "Sessions"),
  ).toBeVisible();
  await expect(
    barLink(page, "Missions"),
  ).toHaveAttribute("aria-current", "page");
  await openMissionRail(page);
  await page.getByRole("searchbox", { name: "Search missions" }).fill("layout");
  await expect(page.getByTestId("rail-mission")).toHaveCount(1);
  if (await page.getByRole("dialog").count())
    await page.keyboard.press("Escape");
  await barLink(page, "Sessions").click();
  const openSessions = page.getByRole("button", {
    name: "Open session list",
    exact: true,
  });
  if (isMobile) {
    await openSessions.click();
    await page.getByRole("dialog").waitFor({ state: "visible" });
    await page.waitForFunction(
      () =>
        document.querySelector("aside.sidebar")!.getBoundingClientRect().x >= 0,
    );
  }
  await page
    .getByRole("searchbox", { name: "Search sessions" })
    .fill("session query");
  await expect(
    page.getByRole("searchbox", { name: "Search sessions" }),
  ).toHaveValue("session query");
  if (await page.getByRole("dialog").count())
    await page.keyboard.press("Escape");
  await expect(
    page.getByRole("searchbox", {
      name: "Search sessions",
      includeHidden: true,
    }),
  ).toHaveValue("session query");
  await barLink(page, "Missions").click();
  await page.goBack();
  await expect(
    barLink(page, "Sessions"),
  ).toHaveAttribute("aria-current", "page");
  if (isMobile) {
    await openSessions.click();
    await page.getByRole("dialog").waitFor({ state: "visible" });
    await page.waitForFunction(
      () =>
        document.querySelector("aside.sidebar")!.getBoundingClientRect().x >= 0,
    );
  }
  await expect(
    page.getByRole("searchbox", { name: "Search sessions" }),
  ).toHaveValue("session query");
  if (await page.getByRole("dialog").count())
    await page.keyboard.press("Escape");
  await page.goForward();
  await openMissionRail(page);
  await expect(
    page.getByRole("searchbox", { name: "Search missions" }),
  ).toHaveValue("layout");
});

test("Context comes first and details collapse inside a fixed workspace", async ({
  page,
}) => {
  await setupSections(page);
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await selectMission(page, "Mission layout");
  await expect(page.getByTestId("console-title")).toHaveText("Mission layout");
  // Below 1400px the details are ONE disclosure above the thread (#948 P3); the Details tab it
  // replaced is gone. At 1400+ the toggle is hidden and the details are already beside the thread.
  const band = page.getByTestId("details-toggle");
  if (await band.isVisible()) await band.click();
  const details = page.getByTestId("mission-details");
  await expect(
    details.getByRole("button", { name: /^Context/ }),
  ).toHaveAttribute("aria-expanded", "true");
  await details.getByRole("button", { name: /^Context/ }).click();
  await expect(
    details.getByRole("button", { name: /^Context/ }),
  ).toHaveAttribute("aria-expanded", "false");
  await expect(
    details.getByRole("button", { name: /^Follow-through/ }),
  ).toBeVisible();
  await expect(
    details.getByRole("button", { name: /^Timeline/ }),
  ).toBeVisible();
  const bounds = await page.evaluate(() => ({
    width: innerWidth,
    height: innerHeight,
    pageWidth: document.documentElement.scrollWidth,
    pageHeight: document.documentElement.scrollHeight,
    overflow: getComputedStyle(
      document.querySelector('[data-testid="mission-console"]')!.parentElement!,
    ).overflowY,
  }));
  expect(bounds.pageWidth).toBeLessThanOrEqual(bounds.width);
  expect(bounds.pageHeight).toBeLessThanOrEqual(bounds.height);
  expect(bounds.overflow).toBe("hidden");
});

for (const width of [1600, 1400, 1280, 801, 800, 412, 375]) {
  test(`mission workspace geometry at ${width}px`, async ({ page }, info) => {
    test.skip(info.project.name !== "desktop", "Explicit viewport matrix");
    await page.setViewportSize({ width, height: 950 });
    await setupSections(page);
    await mockRoster(page); // the manifest-generated roster (#853 P4)
    await page.goto("/mission");
    await selectMission(page, "Mission layout");
    await expect(page.getByTestId("console-title")).toHaveText(
      "Mission layout",
    );
    if (width >= 1400) {
      const thread = (await page.getByTestId("pane").boundingBox())!;
      const details = (await page
        .getByTestId("mission-details")
        .boundingBox())!;
      expect(details.x).toBeGreaterThanOrEqual(thread.x + thread.width);
      expect(details.width).toBeGreaterThan(300);
      await expect(page.getByTestId("details-toggle")).toBeHidden();
    } else {
      // #948 P3: one disclosure instead of a Details TAB. The tab REPLACED the thread (so the
      // composer was hidden); the band opens ABOVE the thread and the composer stays on screen.
      const toggle = page.getByTestId("details-toggle");
      await toggle.click();
      await expect(toggle).toHaveAttribute("aria-expanded", "true");
      await expect(page.getByTestId("mission-details")).toBeVisible();
      await expect(page.getByTestId("composer-input")).toBeVisible();
      const band = (await page.getByTestId("mission-details").boundingBox())!;
      const thread = (await page.getByTestId("pane").boundingBox())!;
      expect(band.y + band.height).toBeLessThanOrEqual(thread.y + 1);
    }
    await info.attach(`mission-${width}`, {
      body: await page.screenshot(),
      contentType: "image/png",
    });
    await page.screenshot({ path: `../design-review/actual-${width}.png` });
    for (const theme of ["light", "dark"]) {
      await page.evaluate((value) => {
        document.documentElement.dataset.theme = value;
      }, theme);
      await page.setViewportSize({ width, height: 540 });
      const geometry = await page.evaluate(() => ({
        width: document.documentElement.scrollWidth,
        height: document.documentElement.scrollHeight,
        left: document.documentElement.scrollLeft,
        top: document.documentElement.scrollTop,
      }));
      expect(geometry).toEqual({ width, height: 540, left: 0, top: 0 });
      await page.getByTestId("detail-context").click();
      await page.getByTestId("detail-timeline").scrollIntoViewIfNeeded();
      await expect(page.getByTestId("detail-timeline")).toBeInViewport();
      expect(
        await page.evaluate(() => document.documentElement.scrollTop),
      ).toBe(0);
      await page.getByTestId("detail-context").click();
    }
  });
}

test("a late search cannot replace the filtered result or the open mission", async ({
  page,
}) => {
  await setupSections(page);
  let release!: () => void;
  const delayed = new Promise<void>((r) => (release = r));
  let oldRequested = false;
  await page.route(/\/api\/missions\?.*$/, async (r) => {
    const q = new URL(r.request().url()).searchParams;
    if (q.get("q") === "old") {
      oldRequested = true;
      await delayed;
    }
    const rows =
      q.get("q") === "infra"
        ? [missionRow({ id: "msn_2", title: "Infra checks", project_id: "p2" })]
        : [missionRow({ title: "Mission layout", project_id: "p1" })];
    await r.fulfill({
      json: {
        ...missionList(rows),
        facets: { projects: ["p1", "p2"], states: ["running"] },
      },
    });
  });
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await selectMission(page, "Mission layout");
  await expect(page.getByTestId("console-title")).toHaveText("Mission layout");
  await openMissionRail(page);
  const search = page.getByRole("searchbox", { name: "Search missions" });
  await search.fill("old");
  await expect.poll(() => oldRequested).toBe(true);
  await search.fill("infra");
  await expect(page.getByTestId("rail-mission")).toHaveCount(1);
  await expect(page.getByTestId("rail-mission")).toContainText("Infra checks");
  release();
  await expect(page.getByTestId("rail-mission")).toContainText("Infra checks");
  await expect(
    page
      .getByRole("combobox", { name: "Filter missions by project" })
      .getByRole("option"),
  ).toHaveCount(3);
  if (await page.getByRole("dialog").count())
    await page.keyboard.press("Escape");
  await expect(page.getByTestId("console-title")).toHaveText("Mission layout");
  await expect(page.getByText(/outside current filters/i)).toBeVisible();
});

for (const initial of ["draft", "planned"]) {
  test(`Begin tracks an attached ${initial} mission without a launch proposal`, async ({
    page,
  }) => {
    await setupSections(page);
    let state = initial;
    const transitions: unknown[] = [];
    let launches = 0;
    await mockMissions(page, {
      missions: missionList([
        missionRow({ state: initial, session_keys: ["claude:worker"] }),
      ]),
    });
    await page.route(/\/api\/missions\/msn_1(?:\?.*)?$/, (r) =>
      r.fulfill({
        json: {
          ...MISSION,
          state,
          sessions: [{ session_key: "claude:worker", removed_at: null }],
          plan: null,
          events: [],
          events_next_seq: null,
        },
      }),
    );
    await page.route("**/api/missions/msn_1/state", (r) => {
      const body = r.request().postDataJSON();
      transitions.push(body);
      state = body.to;
      return r.fulfill({ json: { ...MISSION, state } });
    });
    await page.route("**/api/missions/msn_1/dispatch", (r) => {
      launches++;
      return r.fulfill({ status: 500, json: { detail: "Unexpected launch" } });
    });
    await mockRoster(page); // the manifest-generated roster (#853 P4)
    await page.goto("/mission");
    await selectMission(page);
    await expect(page.getByTestId("mission-begin")).toBeEnabled();
    // Plan again is still offered, behind ⋯ (#967); Begin stays the row's one primary.
    await page.getByTestId("mission-overflow").click();
    await expect(page.getByTestId("mission-replan")).toBeVisible();
    await page.keyboard.press("Escape");
    await page.getByTestId("mission-begin").click();
    await expect(page.getByTestId("mission-state")).toHaveText("running");
    expect(transitions).toEqual(
      initial === "draft"
        ? [
            { from: "draft", to: "planned" },
            { from: "planned", to: "running" },
          ]
        : [{ from: "planned", to: "running" }],
    );
    expect(launches).toBe(0);
    await expect(page.getByTestId("mission-begin")).toHaveCount(0);
  });
}

test("Begin confirms one saved launch and stays disabled until its result is read", async ({
  page,
}, info) => {
  await setupSections(page);
  let state = "planned";
  let launches = 0;
  let release!: () => void;
  const delayed = new Promise<void>((r) => (release = r));
  const plan = {
    plan_id: "pln_1",
    project_id: "p1",
    cwd: "/repo",
    engine: "claude",
    brief: "Reply BEGIN_OK",
    project_options: [{ id: "p1", name: "BattleLab", cwd: "/repo" }],
    engine_options: [{ id: "claude", label: "Claude" }],
  };
  await mockMissions(page, { missions: missionList([missionRow({ state })]) });
  await page.route(/\/api\/missions\/msn_1(?:\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        ...MISSION,
        state,
        plan,
        objectives: [{ key: "ok", title: "Reply BEGIN_OK", gate: true }],
        objectives_state: "done",
        events: [],
        events_next_seq: null,
      },
    }),
  );
  await page.route("**/api/missions/msn_1/dispatch", async (r) => {
    launches++;
    await delayed;
    state = "running";
    await r.fulfill({
      json: { state, session_key: "claude:worker", reason: "" },
    });
  });
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await selectMission(page);
  await page.getByTestId("mission-begin").click();
  await expect(page.getByTestId("mission-begin")).toHaveText("Confirm begin");
  expect(launches).toBe(0);
  expect(
    await page
      .getByTestId("mission-begin")
      .evaluate((el) => el.scrollWidth <= el.clientWidth),
  ).toBe(true);
  // The consequence sits in the PLAN CARD, beside the plan it confirms (#967). It used to sit in the
  // header under Begin, which is what wrapped the header onto a second line. It is on screen, and
  // inside the card rather than clipped by it.
  const consequenceEl = page.getByTestId("mission-dispatch-confirm");
  await expect(consequenceEl).toBeInViewport();
  const consequence = (await consequenceEl.boundingBox())!;
  const card = (await page.getByTestId("mission-plan-card").boundingBox())!;
  expect(consequence.y).toBeGreaterThanOrEqual(card.y);
  expect(consequence.y + consequence.height).toBeLessThanOrEqual(card.y + card.height);
  await page.screenshot({
    path: `../design-review/actual-begin-${info.project.name}.png`,
  });
  await expect(page.getByTestId("mission-dispatch-confirm")).toContainText(
    "/repo",
  );
  await page.getByTestId("mission-begin").click();
  await expect(page.getByTestId("mission-begin")).toBeDisabled();
  // Plan again, behind ⋯ (#967), is held with it.
  await page.getByTestId("mission-overflow").click();
  await expect(page.getByTestId("mission-replan")).toBeDisabled();
  expect(launches).toBe(1);
  release();
  await expect(page.getByTestId("mission-state")).toHaveText("running");
  expect(launches).toBe(1);
});

test("mission project/state filters compose with search and keep distinct project labels", async ({
  page,
}) => {
  await setupSections(page);
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: {
        projects: [
          { id: "p1", name: "Workspace", folders: ["/work/app"] },
          { id: "p2", name: "Workspace", folders: ["/work/infra"] },
        ],
      },
    }),
  );
  const queries: URLSearchParams[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/missions") queries.push(url.searchParams);
  });
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await openMissionRail(page);
  const project = page.getByRole("combobox", {
    name: "Filter missions by project",
  });
  await expect(
    project.getByRole("option", { name: "Workspace · /work/app", exact: true }),
  ).toHaveCount(1);
  await expect(
    project.getByRole("option", {
      name: "Workspace · /work/infra",
      exact: true,
    }),
  ).toHaveCount(1);
  await project.selectOption("p2");
  await page
    .getByRole("combobox", { name: "Filter missions by state" })
    .selectOption("running");
  await page.getByRole("searchbox", { name: "Search missions" }).fill("infra");
  await expect
    .poll(() => {
      const last = queries.at(-1);
      return [last?.get("project"), last?.get("state"), last?.get("q")];
    })
    .toEqual(["p2", "running", "infra"]);
  await expect(page.getByTestId("rail-mission")).toHaveCount(1);
  await expect(page.getByTestId("rail-mission")).toContainText("Infra checks");
  await page
    .getByRole("searchbox", { name: "Search missions" })
    .fill("missing");
  await expect(page.getByTestId("rail-no-missions")).toHaveText(
    "No missions match these filters.",
  );
  await expect(project.getByRole("option")).toHaveCount(3);
  await page
    .getByRole("navigation", { name: "Missions", exact: true })
    .getByRole("button", { name: "Clear mission filters" })
    .click();
  await expect(page.getByTestId("rail-mission")).toHaveCount(2);
});

/** A control can be in the DOM while an overflow ancestor clips its hit target. */
async function expectReachable(page: Page, testId: string) {
  const control = page.getByTestId(testId);
  const clipped = await control.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const problems: string[] = [];
    for (let p = el.parentElement; p; p = p.parentElement) {
      const css = getComputedStyle(p);
      const b = p.getBoundingClientRect();
      if (
        /hidden|clip|auto|scroll/.test(css.overflowY) &&
        (r.top < b.top - 1 || r.bottom > b.bottom + 1)
      )
        problems.push(`${p.tagName}: vertical clipping`);
      if (
        /hidden|clip|auto|scroll/.test(css.overflowX) &&
        (r.left < b.left - 1 || r.right > b.right + 1)
      )
        problems.push(`${p.tagName}: horizontal clipping`);
    }
    for (const [x, y] of [
      [r.left + 4, r.top + 4],
      [r.right - 4, r.bottom - 4],
      [r.left + r.width / 2, r.top + r.height / 2],
    ]) {
      const hit = document.elementFromPoint(x, y);
      if (hit !== el && !el.contains(hit))
        problems.push(`blocked at ${x},${y}`);
    }
    return problems;
  });
  expect(clipped).toEqual([]);
  await control.click({ trial: true });
}

test("maximum mission titles keep the composer reachable on short screens", async ({
  page,
}, info) => {
  test.setTimeout(60_000);
  await setupSections(page);
  let state = "running";
  let title = "Mission ".repeat(25);
  await mockMissions(page, {
    missions: () => missionList([missionRow({ title, state })]),
  });
  await page.route(/\/api\/missions\/msn_1(?:\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        ...MISSION,
        state,
        title,
        events: [],
        events_next_seq: null,
        plan: {
          plan_id: "pln_1",
          project_id: "p1",
          cwd: "/repo",
          engine: "claude",
          brief: "Reply BEGIN_OK",
          project_options: [{ id: "p1", name: "BattleLab", cwd: "/repo" }],
          engine_options: [{ id: "claude", label: "Claude" }],
        },
        objectives: [{ key: "ok", title: "Reply BEGIN_OK", gate: true }],
        objectives_state: "done",
      },
    }),
  );
  for (const phase of ["running", "planned"]) {
    state = phase;
    for (const width of [375, 412, 800, 1280]) {
      await page.setViewportSize({ width, height: 540 });
      await mockRoster(page); // the manifest-generated roster (#853 P4)
      await page.goto("/mission");
      await selectMission(page);
      await expect(page.getByTestId("console-title")).toHaveText(title.trim());
      await page
        .getByTestId("composer-input")
        .fill("Keep this draft reachable");
      await expectReachable(page, "composer-input");
      await expectReachable(page, "composer-send");
      if (state === "planned") {
        await page.getByTestId("mission-begin").click();
        await expect(page.getByTestId("mission-begin")).toHaveText(
          "Confirm begin",
        );
        await expectReachable(page, "composer-input");
        await expectReachable(page, "composer-send");
        await expectReachable(page, "mission-begin");
      }
      const titleButton = page.getByRole("button", {
        name: /Show full mission title/,
      });
      await titleButton.focus();
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", {
        name: "Mission title",
        exact: true,
      });
      await expect(dialog).toContainText(title.trim());
      await page.keyboard.press("Escape");
      await expect(dialog).not.toBeVisible();
      await expect(titleButton).toBeFocused();
      await expectReachable(page, "composer-input");
      await expect(page.getByTestId("composer-input")).toHaveValue(
        "Keep this draft reachable",
      );
    }
  }
  title = "W".repeat(200);
  await page.route(/\/api\/missions\/msn_1(?:\?.*)?$/, (r) =>
    r.fulfill({
      json: { ...MISSION, title, events: [], events_next_seq: null },
    }),
  );
  await page.setViewportSize({ width: 375, height: 540 });
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await selectMission(page);
  await expect(page.getByTestId("console-title")).toHaveText(title);
  await page.getByTestId("composer-input").fill("Still reachable");
  await expectReachable(page, "composer-input");
  await expectReachable(page, "composer-send");
  await page.screenshot({
    path: `../design-review/maximum-title-short-phone-${info.project.name}.png`,
  });
  await info.attach("maximum-title-short-phone", {
    body: await page.screenshot(),
    contentType: "image/png",
  });
});

test("an empty mission search on the landing names the filter, and clearing it restores the rail", async ({
  page,
}) => {
  // This asserted that untracked sessions stayed out of the first-run guidance under an empty
  // search. Both the untracked-session list and the first-run block were removed by #948 P3. What
  // an empty SEARCH must still not do is read as "nothing here": the landing's NEEDS YOU preview
  // says it is scoped to the current filters and offers the way back, and once cleared — with no
  // filter and nothing needing the operator — the preview is absent rather than claiming anything.
  await setupSections(page);
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await openMissionRail(page);
  await page
    .getByRole("searchbox", { name: "Search missions" })
    .fill("no matching mission");
  await expect(page.getByTestId("rail-mission")).toHaveCount(0);
  await expect(page.getByTestId("rail-no-missions")).toHaveText(
    "No missions match these filters.",
  );
  if (await page.getByRole("dialog").count())
    await page.keyboard.press("Escape");
  await expect(page.getByTestId("mission-landing")).toBeVisible();
  await expect(page.getByTestId("landing-needs-scope")).toContainText(
    "in current filters",
  );
  await expect(page.getByTestId("landing-needs-empty")).toHaveText(
    "Nothing needs you in these filters.",
  );
  await page.getByTestId("landing-clear-filters").click();
  await expect(page.getByTestId("landing-needs-you")).toHaveCount(0);
  await openMissionRail(page);
  await expect(page.getByTestId("rail-mission")).toHaveCount(2);
  await expect(
    page.getByRole("searchbox", { name: "Search missions" }),
  ).toHaveValue("");
});

test("a selected later-page mission is not declared outside its matching filters", async ({
  page,
}) => {
  await setupSections(page);
  const rows = Array.from({ length: 101 }, (_, i) =>
    missionRow({ id: `msn_${i + 1}`, title: `Mission ${i + 1}` }),
  );
  await mockMissions(page, {
    missions: (q) => {
      const offset = Number(q.get("offset") ?? 0);
      const limit = Number(q.get("limit") ?? 100);
      return {
        ...missionList(rows.slice(offset, offset + limit)),
        total: rows.length,
        offset,
        limit,
      };
    },
  });
  await page.route(/\/api\/missions\/msn_\d+(?:\?.*)?$/, (r) => {
    const id = new URL(r.request().url()).pathname.split("/").pop()!;
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: rows.find((x) => x.id === id)!.title,
        events: [],
        events_next_seq: null,
      },
    });
  });
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await openMissionRail(page);
  await page
    .getByRole("searchbox", { name: "Search missions" })
    .fill("Mission");
  await page.getByTestId("rail-load-more").click();
  await page
    .getByTestId("rail-mission")
    .filter({ hasText: "Mission 101" })
    .click();
  await expect(page.getByTestId("console-title")).toHaveText("Mission 101");
  // Reach "selected, but not on the loaded page" by CHANGING THE SEARCH: the rail resets to its
  // first page while the selection is kept. (It used to be reached by leaving for Sessions and
  // coming back, which relied on the selection being retained for the visit — #948 P3 made
  // selection plain state, so the section now opens on the landing instead.) The new search
  // still matches Mission 101, so the notice must say it is not LOADED, not that it is filtered out.
  await openMissionRail(page);
  await page
    .getByRole("searchbox", { name: "Search missions" })
    .fill("Mission 1");
  if (await page.getByRole("dialog").count())
    await page.keyboard.press("Escape");
  await expect(page.getByTestId("console-title")).toHaveText("Mission 101");
  await expect(
    page.getByText("Not in the loaded mission results", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText(/Outside current filters/)).toHaveCount(0);
});
