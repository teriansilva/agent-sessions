/** The motion modes in a real browser (#1531, docs/design.md v2 §8), desktop and mobile.
 *
 *  CSS checks alone cannot prove "Off means nothing moves": the button glitch runs a timer, the
 *  HQ portrait a video, and any requestAnimationFrame loop would run too. So these specs count those
 *  callbacks with a shim installed before the app boots, read `document.getAnimations()`, and
 *  switch modes LIVE (the gallery's own Motion control), on a device that does NOT ask to reduce
 *  motion — the case where only the setting can stop things. The atmosphere is CSS only (#1531:
 *  the #211 trail canvas and its rAF loop are retired), so "alive" means its loops are running. */
import { expect, test, type Page } from "@playwright/test";

import { DASHBOARD_PATH, NIGHTJAR_GALLERY_PATH } from "../src/lib/routes";
import { mockNightjarShell, storeMotionChoice } from "./nightjarShell";

declare global {
  interface Window {
    __nj: { raf: number; glitchTimers: number; firstFrame?: { motion?: string; anims: number } };
  }
}

/** Count rAF callbacks and glitch-range timers (7–23 s: the ambient glitch is the only timer in
 *  that range), and record the first painted frame's motion state. Installed before any app code. */
async function installShim(page: Page) {
  await page.addInitScript(() => {
    const nj = (window.__nj = { raf: 0, glitchTimers: 0 } as Window["__nj"]);
    const raf = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (cb: FrameRequestCallback) =>
      raf((t) => {
        nj.raf++;
        cb(t);
      });
    const st = window.setTimeout.bind(window);
    window.setTimeout = ((fn: TimerHandler, ms?: number, ...a: unknown[]) => {
      if (typeof ms === "number" && ms >= 7000 && ms <= 23000) nj.glitchTimers++;
      return st(fn, ms, ...a);
    }) as typeof window.setTimeout;
    raf(() => {
      nj.firstFrame = {
        motion: document.documentElement.dataset.motion,
        anims: document.getAnimations().length,
      };
    });
  });
}

async function counters(page: Page) {
  return page.evaluate(() => ({ ...window.__nj }));
}

/** rAF callbacks the page runs over `ms`, after a settle. */
async function rafOver(page: Page, ms: number): Promise<number> {
  const a = (await counters(page)).raf;
  await page.waitForTimeout(ms);
  return (await counters(page)).raf - a;
}

/** The atmosphere's infinite CSS loops that are running right now (scan drift, sweep, …). */
async function atmoLoops(page: Page): Promise<number> {
  return page.evaluate(
    () =>
      document
        .getAnimations()
        .filter((a) => a.playState === "running" && a.effect?.getComputedTiming().iterations === Infinity)
        .filter((a) => (a.effect as KeyframeEffect).target?.closest?.(".nj-atmo")).length,
  );
}

async function setGalleryMotion(page: Page, label: "Device" | "Full" | "Reduced" | "Off") {
  await page.getByTestId("gallery-motion").getByRole("radio", { name: label }).click();
}

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await mockNightjarShell(page);
  await installShim(page);
});

for (const theme of ["dark", "light"] as const) {
  test(`the backdrop is ONE atmosphere: hex grid, no trail canvas, no square grid (${theme}) (#1531)`, async ({ page }) => {
    await storeMotionChoice(page, "full");
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await page.goto(DASHBOARD_PATH);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await expect(page.getByTestId("nj-atmo")).toBeAttached();
    // The #211 data-flow canvas painted drifting trails over a 48 px square grid; NIGHTJAR's
    // atmosphere replaced it, and both had kept painting, one over the other.
    await expect.soft(page.locator("canvas")).toHaveCount(0);
    const grids = await page.evaluate(() =>
      [document.documentElement, document.body, ...document.querySelectorAll(".nj-atmo, .nj-atmo *")]
        .map((el) => getComputedStyle(el))
        .filter((st) => /48px 48px/.test(st.backgroundSize)).length,
    );
    expect.soft(grids, "no square 48 px grid behind the app").toBe(0);
    const hex = await page.evaluate(() => {
      const st = getComputedStyle(document.querySelector(".nj-atmo .nj-hex")!);
      return { display: st.display, mask: st.webkitMaskImage || st.maskImage };
    });
    expect(hex.display).not.toBe("none");
    expect(hex.mask).toContain("svg");
  });
}

test("Off stored: the first frame already knows it, and nothing moves (#1531)", async ({ page }) => {
  await storeMotionChoice(page, "off");
  await page.goto(DASHBOARD_PATH);
  await expect(page.getByTestId("nj-atmo")).toBeAttached();
  const first = (await counters(page)).firstFrame;
  expect(first?.motion, "data-motion is set before the first frame").toBe("off");
  expect(first?.anims).toBe(0);

  await page.waitForTimeout(800); // settle: the boot reads land
  expect(await rafOver(page, 1500), "no rAF loop in Off").toBe(0);
  expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
  const transitions = await page.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>("*")].filter((el) =>
      getComputedStyle(el).transitionDuration.split(",").some((d) => parseFloat(d) > 0),
    ).length,
  );
  expect(transitions, "every transition is 0s in Off").toBe(0);
  expect((await counters(page)).glitchTimers, "the glitch timer is never armed").toBe(0);
  // The HQ portrait is its poster, never a video.
  expect(await page.locator("video").count()).toBe(0);
});

test("Reduced: no infinite animation, no video, no rAF loop (#1531)", async ({ page }) => {
  await storeMotionChoice(page, "reduced");
  await page.goto(DASHBOARD_PATH);
  await expect(page.getByTestId("nj-atmo")).toBeAttached();
  await page.waitForTimeout(800);
  const infinite = await page.evaluate(
    () => document.getAnimations().filter((a) => a.effect?.getComputedTiming().iterations === Infinity).length,
  );
  expect(infinite).toBe(0);
  expect(await page.locator("video").count()).toBe(0);
  expect(await rafOver(page, 1200)).toBe(0);
});

test("Match device on a reduce-motion device is Reduced (#1531)", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(DASHBOARD_PATH);
  await expect(page.locator("html")).toHaveAttribute("data-motion", "reduced");
});

test("a live switch Full → Off → Full tears the loops down and restores them (#1531)", async ({ page }, info) => {
  await storeMotionChoice(page, "full");
  await page.goto(NIGHTJAR_GALLERY_PATH);
  await expect(page.getByTestId("nightjar-gallery")).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-motion", "full");
  // The atmosphere's loops run on dark in Full (desktop and phone alike).
  expect(await atmoLoops(page), "the atmosphere is alive").toBeGreaterThan(0);
  expect((await counters(page)).glitchTimers, "the glitch timer is armed in Full").toBeGreaterThan(0);
  const loops = await page.evaluate(
    () => document.getAnimations().filter((a) => a.effect?.getComputedTiming().iterations === Infinity).length,
  );
  expect(loops, "the catalog's ambient loops run in Full").toBeGreaterThan(3);

  await setGalleryMotion(page, "Off");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "off");
  await page.waitForTimeout(300);
  expect(await rafOver(page, 1000)).toBe(0);
  expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
  const armedBefore = (await counters(page)).glitchTimers;

  await setGalleryMotion(page, "Full");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "full");
  expect(await atmoLoops(page), "the atmosphere is alive again").toBeGreaterThan(0);
  expect((await counters(page)).glitchTimers, "the glitch timer is re-armed").toBeGreaterThan(armedBefore);
  info.annotations.push({ type: "motion", description: "Full → Off → Full verified" });
});

test("a hidden tab pauses the ambient loops and a visible one resumes them (#1531)", async ({ page }) => {
  await storeMotionChoice(page, "full");
  await page.goto(NIGHTJAR_GALLERY_PATH);
  await expect(page.getByTestId("nightjar-gallery")).toBeVisible();
  expect(await atmoLoops(page), "the atmosphere is alive").toBeGreaterThan(0);

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.locator("html")).toHaveAttribute("data-motion-paused", "hidden");
  await page.waitForTimeout(200);
  expect(await rafOver(page, 800)).toBe(0);
  expect(await atmoLoops(page), "the atmosphere's loops are paused").toBe(0);

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.locator("html")).not.toHaveAttribute("data-motion-paused", /.+/);
  expect(await atmoLoops(page), "the atmosphere is alive again").toBeGreaterThan(0);
});

test("ten idle minutes stop the ambient loops; input resumes them (#1531)", async ({ page }) => {
  await page.clock.install();
  await storeMotionChoice(page, "full");
  await page.goto(NIGHTJAR_GALLERY_PATH);
  await expect(page.getByTestId("nightjar-gallery")).toBeVisible();
  await page.clock.fastForward("10:05");
  await expect(page.locator("html")).toHaveAttribute("data-motion-paused", "idle");
  await page.mouse.move(40, 40);
  await page.mouse.move(60, 60);
  await expect(page.locator("html")).not.toHaveAttribute("data-motion-paused", /.+/);
});

test("streamed text is in the DOM as it arrives and moves nothing above it (#1531)", async ({ page }) => {
  await storeMotionChoice(page, "full");
  await page.goto(NIGHTJAR_GALLERY_PATH);
  const stream = page.getByTestId("g-stream");
  await stream.scrollIntoViewIfNeeded();
  const above = await page.getByTestId("g-panel-default").boundingBox();
  await page.getByRole("button", { name: "Stream an answer" }).click();
  // The first words are there while the stream is still open (the caret rides the end): text is
  // never held back for an animation.
  await expect(stream.locator(".nj-caret")).toBeAttached();
  await expect(stream).toContainText("Two things");
  await expect(stream).toContainText("green again", { timeout: 10_000 });
  await expect(stream.locator(".nj-caret")).toHaveCount(0);
  expect((await page.getByTestId("g-panel-default").boundingBox())?.y).toBe(above?.y);
});

test("the dashboard idles cheaply in Full (main-thread busy < 2% over 5 s) (#1531)", async ({ page, browserName }, info) => {
  test.skip(browserName !== "chromium", "CDP metrics");
  test.skip(info.project.name !== "desktop", "one measurement is enough");
  await storeMotionChoice(page, "full");
  await page.goto(DASHBOARD_PATH);
  await expect(page.getByTestId("nj-atmo")).toBeAttached();
  await page.waitForTimeout(1500);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Performance.enable");
  const read = async () => {
    const { metrics } = await cdp.send("Performance.getMetrics");
    return Object.fromEntries(metrics.map((m) => [m.name, m.value])) as Record<string, number>;
  };
  const a = await read();
  await page.waitForTimeout(5000);
  const b = await read();
  const busy = (b.TaskDuration - a.TaskDuration) / (b.Timestamp - a.Timestamp);
  info.annotations.push({ type: "idle-busy", description: `${(busy * 100).toFixed(2)}%` });
  console.log(`### dashboard idle main-thread busy in Full: ${(busy * 100).toFixed(2)}%`);
  // docs/design.md v2 §8: under 2%. A CPU-starved shared CI runner stretches every task it runs
  // (the same work measures longer there), so CI gets headroom; locally the budget is the design's.
  expect(busy).toBeLessThan(process.env.CI ? 0.04 : 0.02);
});
