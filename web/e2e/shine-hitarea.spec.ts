import { expect, test } from "@playwright/test";

// The `.shine` buttons (incl. "New session") run `hud-btn-glitch`, which animates
// `clip-path: inset(...)`. clip-path removes the clipped region from hit-testing, so a
// click that lands in the clipped part during the animation (fired on every :active press,
// and periodically by ButtonGlitch) misses the button — no navigation. This samples the
// button's hit-area WHILE glitching and asserts every point of the visible button is still
// hittable. Real-browser only: clip-path hit-testing doesn't exist in jsdom.
test("New session keeps its full hit-area during the .shine glitch", async ({ page }) => {
  await page.goto("/");
  // On mobile the New session link lives in the off-canvas drawer — open it so the button is
  // actually on-screen before we measure its hit-area.
  const hamburger = page.getByRole("button", { name: /open session list/i });
  if (await hamburger.isVisible().catch(() => false)) {
    await hamburger.click();
    await expect(page.locator(".app")).toHaveClass(/navOpen/);
    await page.waitForTimeout(350); // let the 0.18s drawer slide-in settle before measuring coords
  }
  const link = page.getByRole("link", { name: /new session/i });
  await expect(link).toBeVisible();
  const misses = await page.evaluate(async () => {
    const el = [...document.querySelectorAll("a")].find((a) => /new session/i.test(a.textContent || ""));
    if (!el) return -1;
    el.classList.add("glitching"); // runs the real hud-btn-glitch keyframes
    const r = el.getBoundingClientRect();
    const x = Math.round(r.left + r.width / 2);
    let miss = 0;
    const t0 = performance.now();
    while (performance.now() - t0 < 360) {
      for (const fy of [0.15, 0.35, 0.5, 0.65, 0.85]) {
        const y = Math.round(r.top + r.height * fy);
        const hit = document.elementFromPoint(x, y);
        if (!(hit === el || el.contains(hit))) miss++;
      }
      await new Promise((res) => setTimeout(res, 8));
    }
    el.classList.remove("glitching");
    return miss;
  });
  console.log(`### hit-area misses during glitch: ${misses}`);
  expect(misses).toBe(0);
});
