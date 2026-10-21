/** The bell's panel is readable and tappable (#1316).
 *
 * The complaint was "fonts are too small, it's too crammed": chrome at 8.4–8.7px, body text at
 * 10.4–11.5px, five controls sharing one header line, a 30px Open link, and — on the light theme — a
 * drawer translucent enough that the page's headings read through the rows. Every one of those is
 * computed style or box geometry, which jsdom cannot measure, so this is a real-browser spec on
 * both projects. The floors are the design doc's: the base 14px body (§4), the `.hud-h` 11px chrome
 * spec, and the 44px touch floor at ≤800px (§8).
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { mockMissions } from "./mission-console";

const NOW = Math.floor(Date.now() / 1000);

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  pulse: { configured: true },
};

const row = (id: string, read: boolean) => ({
  id,
  title: "Awaiting your decision on the PR #20 merge path",
  reason:
    "Hermes approved, but the merge path needs a human call before the agent continues.",
  project: "a-rather-long-project-name-that-must-wrap",
  engine: "claude",
  session_id: `claude:abc${id}`,
  action_id: `act-${id}`,
  ts: NOW - 120,
  read,
});

const SETTLED = [{ ...row("s1", true), title: "Approved: continue the flaky-test fix" }];

async function mockApp(page: Page, theme: "dark" | "light") {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "t" } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  await page.route(/\/api\/pulse\/notifications$/, (r) =>
    r.fulfill({
      json: {
        notifications: [row("1", false), row("2", false), row("3", true)],
        unread: 2,
        uncertain: 0,
        settled: SETTLED,
      },
    }),
  );
  await mockMissions(page);
}

async function openPanel(page: Page): Promise<Locator> {
  await page.goto("/mission");
  await page.getByRole("button", { name: /^Notifications/ }).click();
  const panel = page.getByRole("dialog", { name: "Notifications" });
  await expect(panel.locator("li")).toHaveCount(4);
  await panel.evaluate((el) => Promise.all(el.getAnimations().map((a) => a.finished)));
  return panel;
}

const px = (l: Locator, prop: string) =>
  l.evaluate((el, p) => parseFloat(getComputedStyle(el).getPropertyValue(p)), prop);

for (const theme of ["dark", "light"] as const) {
  test(`type follows the design scale and nothing overflows (${theme})`, async ({ page }) => {
    await mockApp(page, theme);
    const panel = await openPanel(page);
    const first = panel.locator("li").first();

    // Title → detail → meta, each at or above its floor. Measured before #1316: 11.5 / 10.4 / 8.4.
    expect(await px(first.getByText(/^Awaiting your decision/), "font-size")).toBeGreaterThanOrEqual(15);
    expect(await px(first.getByText(/^Hermes approved/), "font-size")).toBeGreaterThanOrEqual(14);
    expect(await px(first.getByText(/ago$/), "font-size")).toBeGreaterThanOrEqual(11);
    // The heading is the `.hud-h` chrome spec (mono 11px), not the 8.7px it was.
    expect(await px(panel.getByText("Notifications", { exact: true }), "font-size")).toBeGreaterThanOrEqual(11);

    // Uncrammed: the list's actions sit on their own row BELOW the heading, not squeezed beside it.
    const heading = (await panel.getByText("Notifications", { exact: true }).boundingBox())!;
    const markAll = (await panel.getByRole("button", { name: "Mark all read" }).boundingBox())!;
    expect(markAll.y).toBeGreaterThanOrEqual(heading.y + heading.height);

    // Breathing room: a row's text is inset at least 12px from the panel edge.
    const pb = (await panel.boundingBox())!;
    const tb = (await first.getByText(/^Awaiting your decision/).boundingBox())!;
    expect(tb.x - pb.x).toBeGreaterThanOrEqual(12);

    // No horizontal overflow, even with a long project name.
    const sw = await panel.evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(sw).toBeLessThanOrEqual(0);
    for (const li of await panel.locator("li").all()) {
      const b = (await li.boundingBox())!;
      expect(b.x + b.width).toBeLessThanOrEqual(pb.x + pb.width + 1);
    }

    // A solid ground: page content must not read through the rows (light-theme drawer was 96%).
    const alpha = await panel.evaluate((el) => {
      const m = getComputedStyle(el).backgroundColor.match(/rgba?\(([^)]+)\)/);
      const parts = m ? m[1].split(",").map((s) => parseFloat(s)) : [];
      return parts.length === 4 ? parts[3] : 1;
    });
    expect(alpha).toBe(1);
  });
}

test("every control in the panel is a real target (44px on a phone)", async ({ page }, testInfo) => {
  await mockApp(page, "dark");
  const panel = await openPanel(page);
  const phone = testInfo.project.name === "mobile";
  const floor = phone ? 44 : 32;
  const first = panel.locator("li").first();

  const targets: Locator[] = [
    panel.getByRole("button", { name: "Mark all read" }),
    panel.getByRole("button", { name: "Clear all" }),
    first.getByRole("button", { name: /^Dismiss:/ }),
    first.getByRole("link", { name: "Open" }),
  ];
  if (phone) targets.push(panel.getByRole("button", { name: "Close notifications" }));
  for (const t of targets) {
    const b = (await t.boundingBox())!;
    expect(b.height, await t.textContent()).toBeGreaterThanOrEqual(floor);
    expect(b.width).toBeGreaterThanOrEqual(floor);
  }
  // `Open` resolves the alert — it is a button-sized target even on desktop, not a 9.6px word.
  const open = (await first.getByRole("link", { name: "Open" }).boundingBox())!;
  expect(open.height).toBeGreaterThanOrEqual(phone ? 44 : 36);
  expect(await px(first.getByRole("link", { name: "Open" }), "font-size")).toBeGreaterThanOrEqual(12);
});

/** Fully inside the panel horizontally, and at least `floor` in BOTH dimensions. */
async function assertTarget(panel: Locator, t: Locator, floor: number) {
  const pb = (await panel.boundingBox())!;
  const b = (await t.boundingBox())!;
  const label = (await t.getAttribute("aria-label")) ?? (await t.textContent());
  expect(b.height, `${label} height`).toBeGreaterThanOrEqual(floor);
  expect(b.width, `${label} width`).toBeGreaterThanOrEqual(floor);
  expect(b.x, `${label} left`).toBeGreaterThanOrEqual(pb.x - 0.5);
  expect(b.x + b.width, `${label} right`).toBeLessThanOrEqual(pb.x + pb.width + 0.5);
}

// The two widths a Pixel 7 does not cover (Hermes on #1316): a narrow phone, where the drawer is
// ~330px wide, and 641–800px, where the panel is still the ANCHORED dropdown but the ≤800px touch
// floor applies. The armed `Clear all?` row and the settled `Clear` are the states that crowd.
for (const vp of [
  { name: "narrow phone (360px, drawer)", width: 360, height: 740, drawer: true },
  { name: "tablet (700px, anchored)", width: 700, height: 900, drawer: false },
]) {
  test(`${vp.name}: every state keeps 44px targets inside the panel`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "one pass per width is enough");
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await mockApp(page, "light");
    const panel = await openPanel(page);
    const first = panel.locator("li").first();

    await assertTarget(panel, panel.getByRole("button", { name: "Mark all read" }), 44);
    await assertTarget(panel, panel.getByRole("button", { name: "Clear all" }), 44);
    await assertTarget(panel, first.getByRole("button", { name: /^Dismiss:/ }), 44);
    await assertTarget(panel, first.getByRole("link", { name: "Open" }), 44);
    await assertTarget(panel, panel.getByTestId("bell-clear-settled"), 44);
    if (vp.drawer)
      await assertTarget(panel, panel.getByRole("button", { name: "Close notifications" }), 44);
    else await expect(panel.getByRole("button", { name: "Close notifications" })).toHaveCount(0);

    // Armed: the confirm row holds its targets, stays on one line under the heading, and fits.
    await panel.getByRole("button", { name: "Clear all" }).click();
    await expect(panel.getByText("Clear all?")).toBeVisible();
    const yes = panel.getByRole("button", { name: "Yes" });
    const cancel = panel.getByRole("button", { name: "Cancel" });
    await assertTarget(panel, yes, 44);
    await assertTarget(panel, cancel, 44);
    const heading = (await panel.getByText("Notifications", { exact: true }).boundingBox())!;
    const yb = (await yes.boundingBox())!;
    const cb = (await cancel.boundingBox())!;
    expect(yb.y).toBeGreaterThanOrEqual(heading.y + heading.height);
    expect(Math.abs(yb.y - cb.y)).toBeLessThanOrEqual(1);
    const sw = await panel.evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(sw).toBeLessThanOrEqual(0);
    await cancel.click();
    await expect(panel.getByRole("button", { name: "Mark all read" })).toBeVisible();
  });
}

test("keyboard reaches Open and shows the accent focus reticle", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "keyboard path is the desktop dropdown");
  await mockApp(page, "dark");
  const panel = await openPanel(page);
  const open = panel.locator("li").first().getByRole("link", { name: "Open" });
  // Start inside the panel and Tab through it, so `:focus-visible` (a keyboard heuristic) applies.
  // The panel is portalled to <body>, so a Tab from the bell walks the rest of the page first;
  // that pre-existing tab order is not this layout pass's to change.
  await panel.getByRole("button", { name: "Mark all read" }).focus();
  for (let i = 0; i < 6; i++) {
    if (await open.evaluate((el) => el === document.activeElement)) break;
    await page.keyboard.press("Tab");
  }
  await expect(open).toBeFocused();
  const outline = await open.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { style: cs.outlineStyle, width: parseFloat(cs.outlineWidth) };
  });
  expect(outline.style).toBe("solid");
  expect(outline.width).toBeGreaterThanOrEqual(1.5);
});
