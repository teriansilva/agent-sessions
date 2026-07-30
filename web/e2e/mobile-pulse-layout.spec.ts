import { expect, test, type Page } from "@playwright/test";

// #750 — the #726 Pulse surfaces on a phone.
//
// Both defects here are pure box geometry, which is exactly what jsdom cannot model: a
// `position: absolute; right: 0` dropdown laid out at x=-50, and a CTA that lands inline
// beside body text on a wrapped flex line. Unit tests were green through both. The numbers in
// the assertions below are the ones measured on the emulated phone before the fix.

const NOW = Math.floor(Date.now() / 1000);

const ORCH_CONFIG = {
  enabled: true,
  autonomy: "suggest",
  allowed_verbs: ["continue"],
  auto_verbs_ceiling: ["continue"],
  confidence_min: 0.75,
  interval_minutes: 10,
  max_actions_per_pass: 4,
  proposal_ttl_minutes: 30,
  nudge_template: "Please continue.",
  prompt: "system prompt",
  notify: "escalations",
  configured: true,
  default_prompt: "system prompt",
  default_nudge_template: "Please continue.",
};

const ESCALATE_ACTION = {
  id: "act-escalate",
  state: "escalated",
  ts: NOW,
  expires_at: NOW + 1800,
  tier: "suggest",
  session_id: "codex:bbbbbbbb-0000-4000-8000-000000000002",
  engine: "codex",
  title: "Fix flaky #734 to unblock PR #745 merge",
  project: "battlelab",
  project_id: "p1",
  verb: "escalate",
  confidence: 0.8,
  rationale:
    "Flaky test fix is done; merge decision for PR #745 needs user input.",
  evidence: "screen output",
};

// Long, realistic rows: the bug is only visible with content wide enough to be clipped.
const NOTIFICATIONS = Array.from({ length: 6 }, (_, i) => ({
  id: `n${i}`,
  title: "Awaiting user decision on PR #20 merge path",
  reason: "PR #20 merge path decision requires user input after approval.",
  project: "battlelab",
  engine: "claude",
  session_id: "claude:abc",
  action_id: `a${i}`,
  ts: NOW - 60,
  read: false,
}));

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        pulse: {
          auto_enabled: false,
          interval_minutes: 30,
          window_days: 3,
          scan_depth: "slow",
          configured: true,
        },
        orchestrator: ORCH_CONFIG,
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 2,
        generated_at: null,
        window_days: 3,
        scan_depth: "slow",
        input_fingerprint: null,
        synthesis_skipped: false,
        banner: null,
        cards: [],
      },
    }),
  );
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: {
        config: ORCH_CONFIG,
        pending: [ESCALATE_ACTION],
        feed: [],
        expired_now: 0,
        running: [],
        last: {},
      },
    }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: NOTIFICATIONS, unread: 120 } }),
  );
});

/** Let the slide-in finish before measuring — the drawer's box at t=0 is deliberately
 *  off-screen (`translateX(100%)`), so an immediate read would assert against the animation
 *  rather than the layout. */
async function settled(page: Page, selector: string) {
  await page
    .locator(selector)
    .evaluate((el) => Promise.all(el.getAnimations().map((a) => a.finished)));
}

const openBell = async (page: Page) => {
  await page.goto("/");
  await page.getByRole("button", { name: /notifications/i }).click();
};

test("the bell sits at the right edge of the topbar, not stranded mid-bar", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile layout");
  await page.goto("/");
  const vw = page.viewportSize()!.width;
  const box = (await page
    .getByRole("button", { name: /notifications/i })
    .boundingBox())!;
  // Measured at 122px of dead space to the right of the bell before the fix, because the
  // cluster was carried right only by `.hud-telemetry`'s auto margin — and the telemetry is
  // display:none at this width.
  expect(vw - (box.x + box.width)).toBeLessThanOrEqual(24);
});

test("the notification panel opens fully on-screen as a right-hand drawer", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile layout");
  await openBell(page);
  const vp = page.viewportSize()!;
  const panel = page.getByRole("dialog", { name: /notifications/i });
  await expect(panel).toBeVisible();
  await settled(page, '[role="dialog"][aria-label="Notifications"]');

  const box = (await panel.boundingBox())!;
  // The whole point: before the fix this was x=-50 with 50px of every row off-screen left.
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(vp.width + 1);
  // Full-height drawer, mirroring the session sidebar — and proof it is NOT clipped to the
  // topbar, which sets backdrop-filter and would contain a non-portalled fixed element.
  expect(box.height).toBeGreaterThan(vp.height * 0.8);

  // No row is clipped, and the page itself never gains a horizontal scroll.
  for (const row of await panel.locator("li").all()) {
    const rb = (await row.boundingBox())!;
    expect(rb.x).toBeGreaterThanOrEqual(0);
    expect(rb.x + rb.width).toBeLessThanOrEqual(vp.width + 1);
  }
  const doc = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }));
  expect(doc.sw).toBeLessThanOrEqual(doc.cw);
});

test("tapping inside the drawer keeps it open; the scrim closes it", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile layout");
  await openBell(page);
  const panel = page.getByRole("dialog", { name: /notifications/i });
  await settled(page, '[role="dialog"][aria-label="Notifications"]');

  // The drawer is portalled to <body>, so every tap in it is outside the bell's wrapper. If
  // the close test still keyed off the wrapper alone, this tap would dismiss the drawer.
  await panel
    .getByText("Awaiting user decision on PR #20 merge path")
    .first()
    .click();
  await expect(panel).toBeVisible();

  // Tap the exposed dimmed strip to the left of the drawer — the scrim spans the viewport but
  // its centre is under the drawer, and a user dismisses by tapping what they can actually see.
  const scrim = page.getByRole("button", { name: /dismiss notifications/i });
  await expect(scrim).toBeVisible();
  await page.mouse.click(20, 400);
  await expect(panel).toBeHidden();
});

// The drawer sets `aria-modal="true"`. That is a promise about the interaction model, and an
// unkept one is worse than no promise at all — it tells a screen reader the background is
// inert while it is still reachable. These assert the promise, not the attribute.
test("the drawer takes focus, contains it, and hands it back to the bell", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile layout");
  await openBell(page);
  const panel = page.getByRole("dialog", { name: /notifications/i });
  await settled(page, '[role="dialog"][aria-label="Notifications"]');

  const inDialog = () =>
    page.evaluate(() => {
      const d = document.querySelector(
        '[role="dialog"][aria-label="Notifications"]',
      );
      return (
        !!d && !!document.activeElement && d.contains(document.activeElement)
      );
    });

  // Focus moved INTO the drawer on open — Hermes measured activeInsideDialog=false here.
  expect(await inDialog()).toBe(true);

  // …and Tab keeps it there instead of walking out into the page behind the scrim.
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press("Tab");
    expect(await inDialog()).toBe(true);
  }

  // The background is genuinely isolated, not merely covered by a scrim.
  expect(
    await page.evaluate(() =>
      document.getElementById("root")?.hasAttribute("inert"),
    ),
  ).toBe(true);

  // Escape closes and focus returns to the control that opened it. The restore is deliberately
  // deferred a frame (see the component), so this waits for the contract instead of sampling
  // `activeElement` once — a single sample races the queued frame and fails intermittently
  // under parallel workers.
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await expect(
    page.getByRole("button", { name: /notifications/i }),
  ).toBeFocused();
  // …and the isolation is lifted, or the whole app would stay dead.
  await expect
    .poll(() =>
      page.evaluate(() =>
        document.getElementById("root")?.hasAttribute("inert"),
      ),
    )
    .toBe(false);
});

test("dismissing via the scrim also restores focus and lifts the isolation", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile layout");
  await openBell(page);
  await settled(page, '[role="dialog"][aria-label="Notifications"]');
  await page.mouse.click(20, 400);
  await expect(
    page.getByRole("dialog", { name: /notifications/i }),
  ).toBeHidden();
  await expect
    .poll(() =>
      page.evaluate(() =>
        document.getElementById("root")?.hasAttribute("inert"),
      ),
    )
    .toBe(false);
  // Auto-retrying for the same reason as the Escape path — and this is the path where the
  // deferral actually matters, since the browser is still settling focus from the tap.
  await expect(
    page.getByRole("button", { name: /notifications/i }),
  ).toBeFocused();
});

test("desktop keeps the anchored dropdown under the bell", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop layout");
  await page.goto("/");
  const trigger = page.getByRole("button", { name: /notifications/i });
  const tb = (await trigger.boundingBox())!;
  await trigger.click();
  const panel = page.getByRole("dialog", { name: /notifications/i });
  const pb = (await panel.boundingBox())!;

  // Right edges line up: still `position: absolute; right: 0` on the wrap, not a drawer.
  expect(Math.abs(pb.x + pb.width - (tb.x + tb.width))).toBeLessThanOrEqual(2);
  expect(pb.y).toBeGreaterThanOrEqual(tb.y + tb.height);
  // …and it is still rendered inside the bell's wrapper rather than portalled to <body>.
  await expect(
    page.locator(
      '[data-topbar-keep] [role="dialog"][aria-label="Notifications"]',
    ),
  ).toHaveCount(1);
});

test("'Run now' takes its own full-width row instead of sitting inline with the threshold text", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile layout");
  await page.goto("/pulse");
  await expect(
    page.getByRole("heading", { name: /needs a decision/i }),
  ).toBeVisible();

  const run = page.getByRole("button", { name: /run now/i });
  const rb = (await run.boundingBox())!;
  const rowBox = await page.evaluate(() => {
    const el = document.querySelector('[class*="meterRow"]')!;
    const r = el.getBoundingClientRect();
    return { x: r.x, width: r.width, bottom: r.bottom };
  });

  // Before the fix: 86px wide at x=208, wedged beside "conf ≥ 0.75 · below → escalate" on the
  // wrapped second line. Now it spans the row and owns its own line.
  expect(rb.width).toBeGreaterThan(rowBox.width * 0.9);
  expect(rb.height).toBeGreaterThanOrEqual(44);

  // Nothing else shares its line — the collision is what made the block unreadable.
  const overlaps = await page.evaluate(() => {
    const btn = Array.from(document.querySelectorAll("button")).find((b) =>
      /run now/i.test(b.textContent || ""),
    )!;
    const br = btn.getBoundingClientRect();
    return Array.from(document.querySelectorAll('[class*="meterRow"] > *'))
      .filter((el) => el !== btn)
      .filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.bottom > br.top + 1 && r.top < br.bottom - 1;
      }).length;
  });
  expect(overlaps).toBe(0);
});
