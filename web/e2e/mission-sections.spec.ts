import { expect, test, type Page } from "@playwright/test";
import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";

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

test("section buttons share the brand row and Send typography at every width (#946)", async ({
  page,
}) => {
  await setupSections(page);
  await page.goto("/pulse");
  await expect(page.getByTestId("console-title")).toHaveText("Mission layout");
  for (const width of [320, 360, 375, 412, 640, 641, 800, 801, 1100, 1440]) {
    await page.setViewportSize({ width, height: 740 });
    const header = page.locator(".hud-topbar");
    const brand = page.locator(".hud-brand");
    const nav = page.getByRole("navigation", { name: "Main sections" });
    const hb = (await header.boundingBox())!;
    const bb = (await brand.boundingBox())!;
    expect(hb.height, `header at ${width}`).toBeLessThanOrEqual(52);
    const sendStyle = await page.getByTestId("composer-send").evaluate((el) => {
      const s = getComputedStyle(el);
      return [
        s.fontFamily,
        s.fontSize,
        s.fontWeight,
        s.letterSpacing,
        s.minHeight,
      ];
    });
    for (const name of ["Sessions", "Missions"]) {
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
      expect(
        await link.evaluate((el) => {
          const s = getComputedStyle(el);
          return [
            s.fontFamily,
            s.fontSize,
            s.fontWeight,
            s.letterSpacing,
            s.minHeight,
          ];
        }),
      ).toEqual(sendStyle);
      await link.click({ trial: true });
    }
    await page.locator(".hud-topbar > .navToggle").click({ trial: true });
    await page
      .locator(".hud-topbar [data-topbar-keep] button")
      .click({ trial: true });
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
  await page.goto("/pulse");
  await expect(page.getByTestId("console-title")).toHaveText("Mission layout");
  await page.getByTestId("composer-input").fill("Keep this draft");
  const controls = [
    page.locator(".hud-topbar > .navToggle"),
    page.getByRole("button", { name: "Show full mission title" }),
    page.getByTestId("composer-send"),
    page.getByRole("link", { name: "Sessions", exact: true }),
    page.getByRole("link", { name: "Missions", exact: true }),
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
  await page.getByRole("link", { name: "Sessions", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Sessions", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await page.getByRole("link", { name: "Missions", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Missions", exact: true }),
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
  await page.goto("/pulse");
  await expect(page.getByTestId("console-title")).toHaveText("Mission layout");
  const toggle = page.locator(".hud-topbar > .navToggle");
  await page.clock.fastForward(7001);
  await expect(toggle).toHaveClass(/glitching/);
  await page.clock.fastForward(301);
  await expect(toggle).not.toHaveClass(/glitching/);

  const missions = page.getByRole("link", { name: "Missions", exact: true });
  // The next ambient selection must skip a disabled ordinary button in the real DOM.
  await toggle.evaluate((el) => ((el as HTMLButtonElement).disabled = true));
  await page.clock.fastForward(7001);
  await expect(toggle).not.toHaveClass(/glitching/);
  await expect(
    page.getByRole("link", { name: "Sessions", exact: true }),
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
    await page.goto("/pulse");
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
      await page.getByTestId("drawer-close").click();
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
  await page.goto("/pulse");
  await expect(
    page.getByRole("link", { name: "Sessions", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Missions", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await openMissionRail(page);
  await page.getByRole("searchbox", { name: "Search missions" }).fill("layout");
  await expect(page.getByTestId("rail-mission")).toHaveCount(1);
  if (await page.getByRole("dialog").count())
    await page.keyboard.press("Escape");
  await page.getByRole("link", { name: "Sessions", exact: true }).click();
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
  await page.getByRole("link", { name: "Missions", exact: true }).click();
  await page.goBack();
  await expect(
    page.getByRole("link", { name: "Sessions", exact: true }),
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
  await page.goto("/pulse");
  await expect(page.getByTestId("console-title")).toHaveText("Mission layout");
  const tab = page.getByRole("tab", { name: "Details", exact: true });
  if (await tab.isVisible()) await tab.click();
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
    await page.goto("/pulse");
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
      await expect(page.getByTestId("stop-details")).toBeHidden();
    } else {
      await page.getByTestId("stop-details").click();
      await expect(page.getByTestId("composer-input")).toBeHidden();
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
  await page.goto("/pulse");
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
    await page.goto("/pulse");
    await expect(page.getByTestId("mission-begin")).toBeEnabled();
    await expect(page.getByTestId("mission-replan")).toBeVisible();
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
  await page.goto("/pulse");
  await page.getByTestId("mission-begin").click();
  await expect(page.getByTestId("mission-begin")).toHaveText("Confirm begin");
  expect(launches).toBe(0);
  expect(
    await page
      .getByTestId("mission-begin")
      .evaluate((el) => el.scrollWidth <= el.clientWidth),
  ).toBe(true);
  const consequence = (await page
    .getByTestId("mission-dispatch-confirm")
    .boundingBox())!;
  const tabs = (await page
    .getByRole("tablist", { name: "Mission view" })
    .boundingBox())!;
  expect(consequence.y + consequence.height).toBeLessThanOrEqual(tabs.y);
  await page.screenshot({
    path: `../design-review/actual-begin-${info.project.name}.png`,
  });
  await expect(page.getByTestId("mission-dispatch-confirm")).toContainText(
    "/repo",
  );
  await page.getByTestId("mission-begin").click();
  await expect(page.getByTestId("mission-begin")).toBeDisabled();
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
  await page.goto("/pulse");
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
      await page.goto("/pulse");
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
  await page.goto("/pulse");
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

test("an empty mission search keeps untracked sessions out of first-run guidance", async ({
  page,
}) => {
  await setupSections(page);
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        generated_at: 1,
        window_days: 3,
        cards: [
          {
            id: "claude:11111111-1111-1111-1111-111111111111",
            engine: "claude",
            title: "Untracked work",
            cwd: "/repo",
            project: { kind: "project", id: "p1", name: "BattleLab" },
            last_activity: 1,
            live: true,
            state: "in_flight",
            mission_id: null,
          },
        ],
      },
    }),
  );
  await page.goto("/pulse");
  await openMissionRail(page);
  await page
    .getByRole("button", { name: /Sessions without a mission/ })
    .click();
  await openMissionRail(page);
  await page
    .getByRole("searchbox", { name: "Search missions" })
    .fill("no matching mission");
  await expect(page.getByTestId("rail-mission")).toHaveCount(0);
  if (await page.getByRole("dialog").count())
    await page.keyboard.press("Escape");
  await expect(page.getByTestId("first-run")).toHaveCount(0);
  const empty = page.getByTestId("mission-filter-empty");
  await expect(empty).toContainText("No missions match these filters");
  await expect(page.getByTestId("untracked-session")).toContainText(
    "Untracked work",
  );
  await empty.getByRole("button", { name: "Clear mission filters" }).click();
  await expect(empty).toHaveCount(0);
  await openMissionRail(page);
  await expect(page.getByTestId("rail-mission")).toHaveCount(2);
});

test("a restored later-page mission is not declared outside its matching filters", async ({
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
  await page.goto("/pulse");
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
  await page.getByRole("link", { name: "Sessions", exact: true }).click();
  await page.getByRole("link", { name: "Missions", exact: true }).click();
  await expect(page.getByTestId("console-title")).toHaveText("Mission 101");
  await expect(
    page.getByText("Not in the loaded mission results", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText(/Outside current filters/)).toHaveCount(0);
});
