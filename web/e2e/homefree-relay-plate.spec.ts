/** The relay connection as a top-bar plate (#1658), in a real browser.
 *
 *  Streamed through the Home Free relay, the connect shell used to float its connection bar over
 *  the middle of the app's top bar. Now the shell publishes the connection and the app's top bar
 *  shows it as an LED plate left of HQ; the floating bar is only a fallback for an app with no top
 *  bar. Overlap, stacking, the phone sheet, hit boxes at 320px and real event dispatch are exactly
 *  what jsdom cannot see, so this drives the real connect page with the REAL SPA mounted in it.
 *
 *  The harness stands in for the tunnel only: its `mountApp` renders the real app through the
 *  `render` the page hands it (the real mountApp's own default) and lets the test fire the relay's
 *  events — `warn` before or after the mount, and `closed`. The app's API is mocked at the network
 *  edge, as the other shell specs do.
 */
import { expect, test, type Page } from "@playwright/test";

import { verifyHuman } from "./connect-helpers";
import { connectRelayed, fire, mockApi, RELAY_HOST_LAUNCH } from "./relayHarness";
import { TOUCH_FLOOR } from "./touchTarget";

test.use(RELAY_HOST_LAUNCH);

test("connected: the connection is an LED plate left of HQ, and nothing floats over the app", async ({
  page,
  baseURL,
}) => {
  await connectRelayed(page, baseURL);
  const plate = page.getByTestId("relay-plate");
  const hq = page.getByTestId("ask-toggle");
  // The name carries the state, never the countdown: focus parks here, and a name that changed
  // every second would be re-announced every second. The time left is the plate's description
  // and tooltip, to the minute.
  await expect(plate).toHaveAccessibleName("Relay connection: linked — onyx-9780");
  await expect(plate).toHaveAccessibleDescription(/^3 h 5\d min left$/);
  await expect(plate).toHaveAttribute("title", /^Relay connection: linked — onyx-9780 · 3 h 5\d min left$/);
  await expect(page.getByTestId("relay-led")).toHaveClass(/\bup\b/);
  // In the corner, immediately before HQ.
  const pb = (await plate.boundingBox())!;
  const hb = (await hq.boundingBox())!;
  expect(pb.x + pb.width).toBeLessThanOrEqual(hb.x);
  expect(Math.abs(pb.y - hb.y)).toBeLessThan(2);
  expect(pb.height).toBeCloseTo(hb.height, 0);

  // The shell's floating bar stands down — and stays down past its 2 s fallback window.
  await page.waitForTimeout(2500);
  await expect(page.locator("#session-box")).toBeHidden();
  // Nothing of the shell sits over the top bar: its centre and the plate's own centre are the app's.
  const owner = await page.evaluate(() => {
    const bar = document.querySelector(".hud-topbar")!.getBoundingClientRect();
    const hit = document.elementFromPoint(bar.left + bar.width / 2, bar.top + bar.height / 2);
    return hit?.closest(".hud-topbar") ? "app" : (hit?.closest("#session-box") ? "shell" : "other");
  });
  expect(owner).toBe("app");
});

test("the plate opens the panel (a sheet on a phone); Escape, the plate and a press outside close it, focus back on the plate", async ({
  page,
  baseURL,
}) => {
  await connectRelayed(page, baseURL);
  const plate = page.getByTestId("relay-plate");
  const panel = page.getByTestId("relay-panel");
  const phone = page.viewportSize()!.width <= 800;

  await expect(panel).toHaveCount(0); // closed by default
  await plate.click();
  await expect(panel).toBeVisible();
  await expect(plate).toHaveAttribute("aria-expanded", "true");
  if (phone) await expect(panel).toHaveAttribute("data-sheet", "");
  await expect(page.getByTestId("relay-state")).toContainText(/linked — onyx-9780/i);
  await expect(page.getByTestId("relay-status")).toContainText("connected — streaming your BattleLab");
  await expect(page.getByTestId("relay-head")).toContainText(/Session \d.* left/);
  await expect(page.getByTestId("relay-sign-out")).toBeFocused();
  // The one item is described by the header, so its state and time are heard on focus.
  await expect(page.getByTestId("relay-sign-out")).toHaveAccessibleDescription(/linked — onyx-9780/i);

  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(plate).toBeFocused();

  if (!phone) {
    // The plate again closes it.
    await plate.click();
    await expect(panel).toBeVisible();
    await plate.click();
    await expect(panel).toHaveCount(0);
  }

  // A press outside closes it. On a phone the sheet spans the width, so outside is its scrim.
  await plate.click();
  await expect(panel).toBeVisible();
  if (phone) await page.getByTestId("relay-panel-scrim").click({ position: { x: 8, y: 8 } });
  else await page.mouse.click(8, page.viewportSize()!.height - 8);
  await expect(panel).toHaveCount(0);
  await expect(plate).toBeFocused();
});

test("Sign out in the panel signs out — the click lands, the app and its plate are gone", async ({
  page,
  baseURL,
}) => {
  await connectRelayed(page, baseURL);
  await page.getByTestId("relay-plate").click();
  const item = page.getByTestId("relay-sign-out");
  // The topmost element at the item's centre is the item: the panel is not painted over (#752).
  // Polled: the phone sheet slides up, so its first measured box is mid-flight.
  await expect
    .poll(async () => {
      const box = (await item.boundingBox())!;
      return page.evaluate(
        ([x, y]) => !!document.elementFromPoint(x, y)?.closest('[data-testid="relay-sign-out"]'),
        [box.x + box.width / 2, box.y + box.height / 2],
      );
    })
    .toBe(true);
  await item.click();
  await expect(page.locator("body")).toHaveAttribute("data-state", "signed-out");
  await expect(page.getByRole("button", { name: "Connect" })).toBeVisible();
  await expect(page.getByTestId("relay-plate")).toHaveCount(0);
  await expect(page.locator("#session-box")).toBeHidden();
});

test("warn turns the LED amber, and the name, the tooltip and the panel say the session ends soon", async ({
  page,
  baseURL,
}) => {
  await connectRelayed(page, baseURL);
  await fire(page, { type: "warn", remaining: 120 });
  const plate = page.getByTestId("relay-plate");
  await expect(page.getByTestId("relay-led")).toHaveClass(/\battention\b/);
  await expect(plate).toHaveAccessibleName(/^Relay connection: session ends soon — onyx-9780/);
  await expect(plate).toHaveAttribute("title", /session ends soon/);
  await plate.click();
  await expect(page.getByTestId("relay-state")).toContainText(/session ends soon/i);
  await expect(page.getByTestId("relay-status")).toContainText("session ends soon (120s)");
});

test("a warn that lands while the app is still mounting is kept: amber, and no 'connected' line", async ({
  page,
  baseURL,
}) => {
  await connectRelayed(page, baseURL, { warnEarly: true });
  const plate = page.getByTestId("relay-plate");
  await expect(page.getByTestId("relay-led")).toHaveClass(/\battention\b/);
  await expect(plate).toHaveAttribute("title", /session ends soon/);
  await expect(plate).toHaveAccessibleName(/session ends soon/);
  await plate.click();
  await expect(page.getByTestId("relay-state")).toContainText(/session ends soon/i);
  await expect(page.getByTestId("relay-status")).toContainText("session ends soon (240s)");
  await expect(page.getByTestId("relay-status")).not.toContainText("connected");
});

test("a closed link returns to the connect card with no plate left behind", async ({ page, baseURL }) => {
  await connectRelayed(page, baseURL);
  await fire(page, { type: "closed" });
  await expect(page.locator("body")).toHaveAttribute("data-state", "signed-out");
  await expect(page.locator("#status")).toContainText("disconnected");
  await expect(page.getByTestId("relay-plate")).toHaveCount(0);
  await expect(page.locator("#session-box")).toBeHidden();
});

test("an ended attempt leaves nothing behind: the next attempt shows no plate until it is connected itself", async ({
  page,
  baseURL,
}) => {
  await connectRelayed(page, baseURL);
  await fire(page, { type: "warn", remaining: 60 });
  await fire(page, { type: "closed" });
  await expect(page.locator("body")).toHaveAttribute("data-state", "signed-out");

  // The next attempt renders its app but is held in its mount — not connected, nothing published.
  await page.evaluate(() => {
    const w = window as unknown as { __holdMount?: Promise<void>; __releaseMount?: () => void };
    w.__holdMount = new Promise<void>((r) => (w.__releaseMount = r));
  });
  await page.getByLabel("Console key").fill("onyx-9780");
  await page.getByLabel("Access password").fill("stream-secret");
  await verifyHuman(page);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.locator(".hud-topbar")).toBeAttached({ timeout: 20_000 });
  await expect(page.locator("body")).toHaveAttribute("data-state", "connecting");
  // The ended attempt's amber snapshot is gone: no plate, and nothing for a plate to sign out of.
  await expect(page.getByTestId("relay-plate")).toHaveCount(0);

  await page.evaluate(() => (window as unknown as { __releaseMount: () => void }).__releaseMount());
  await expect(page.locator("body")).toHaveAttribute("data-state", "connected");
  await expect(page.getByTestId("relay-led")).toHaveClass(/\bup\b/);
  await expect(page.getByTestId("relay-plate")).toHaveAccessibleName(/^Relay connection: linked/);
});

test("a plate that leaves while still connected brings the shell's bar back at once — Sign out stays reachable", async ({
  page,
  baseURL,
}) => {
  await connectRelayed(page, baseURL);
  // Inside the 2 s first-dock window, so it is the undock that shows the bar, not the timer.
  await page.evaluate(() => (window as unknown as { __unmountApp: () => void }).__unmountApp());
  await expect(page.locator("#session-box")).toBeVisible({ timeout: 800 });
  await expect(page.locator("body")).toHaveAttribute("data-state", "connected");
});

test("reduced motion: the LED is steady and the state word is still in the name", async ({ page, baseURL }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await connectRelayed(page, baseURL);
  const led = page.getByTestId("relay-led");
  await expect.poll(() => led.evaluate((el) => getComputedStyle(el).animationName)).toBe("none");
  await expect(page.getByTestId("relay-plate")).toHaveAccessibleName(/linked/);
});

/** Every visible top-bar control's hit box, left to right. */
async function barControls(page: Page) {
  return page.evaluate(() => {
    const bar = document.querySelector(".hud-topbar") as HTMLElement;
    const boxes = [...bar.querySelectorAll<HTMLElement>("a, button")]
      .filter((e) => e.offsetWidth > 0 && e.getBoundingClientRect().top < bar.getBoundingClientRect().bottom)
      .map((e) => {
        const r = e.getBoundingClientRect();
        // A control may paint its touch target as an absolute ::after halo (the hamburger: 32px
        // with inset -6px = 44px). Its hit HEIGHT includes the halo; overlap is judged on the box.
        const halo = getComputedStyle(e, "::after");
        const grow =
          halo.content !== "none" && halo.position === "absolute"
            ? Math.max(0, -parseFloat(halo.top) || 0) + Math.max(0, -parseFloat(halo.bottom) || 0)
            : 0;
        return {
          name: e.getAttribute("aria-label") ?? e.textContent?.trim() ?? "",
          left: r.left,
          right: r.right,
          height: r.height + grow,
        };
      })
      .sort((a, b) => a.left - b.left);
    return {
      boxes,
      tile: getComputedStyle(bar).getPropertyValue("--tb-tile").trim(),
      scroll: bar.scrollWidth - bar.clientWidth,
      vw: window.innerWidth,
    };
  });
}

test.describe("the relayed phone bar fits on a sidebar route", () => {
  for (const width of [320, 344, 360, 390, 412, 441, 460]) {
    test(`${width}px: no two controls overlap, all ≥44px tall, no overflow, the relayed tile size`, async ({
      page,
      baseURL,
    }, info) => {
      test.skip(info.project.name !== "mobile", "a phone layout");
      await page.setViewportSize({ width, height: 800 });
      await connectRelayed(page, baseURL);
      // A sidebar route — the hamburger is in the row — reached the way an operator does.
      await page.getByRole("link", { name: /sessions/i }).first().click();
      await expect(page.locator(".hud-topbar .navToggle")).toBeVisible();
      const m = await barControls(page);
      for (let i = 1; i < m.boxes.length; i++)
        expect(m.boxes[i].left, `${m.boxes[i - 1].name} runs under ${m.boxes[i].name}`).toBeGreaterThanOrEqual(
          m.boxes[i - 1].right - 0.5,
        );
      for (const b of m.boxes) expect(b.height, b.name).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      expect(m.boxes[m.boxes.length - 1].right).toBeLessThanOrEqual(m.vw);
      expect(m.scroll).toBeLessThanOrEqual(0);
      expect(m.tile).toBe(width <= 380 ? "26px" : width <= 440 ? "29px" : "34px");
    });
  }
});

test("the first desktop widths fit the plate too (801–880px gives the tabs' padding back)", async ({
  page,
  baseURL,
}, info) => {
  test.skip(info.project.name !== "desktop", "a desktop layout");
  await connectRelayed(page, baseURL);
  await page.getByRole("link", { name: /sessions/i }).first().click();
  await expect(page.locator(".hud-topbar .navToggle")).toBeVisible();
  for (const width of [801, 820, 860, 881]) {
    await page.setViewportSize({ width, height: 800 });
    const m = await barControls(page);
    for (let i = 1; i < m.boxes.length; i++)
      expect(m.boxes[i].left, `${width}px: ${m.boxes[i - 1].name} runs under ${m.boxes[i].name}`).toBeGreaterThanOrEqual(
        m.boxes[i - 1].right - 0.5,
      );
    expect(m.boxes[m.boxes.length - 1].right).toBeLessThanOrEqual(m.vw);
  }
});

test("the plain app has no plate and keeps today's tile size", async ({ page }, info) => {
  await mockApi(page);
  await page.setViewportSize({ width: 412, height: 800 });
  await page.goto("/");
  await expect(page.getByTestId("operator-menu")).toBeVisible();
  await expect(page.getByTestId("relay-plate")).toHaveCount(0);
  if (info.project.name === "mobile") {
    const m = await barControls(page);
    expect(m.tile).toBe("32px");
  }
});
