import { expect, test, type Page } from "@playwright/test";
import { NEW_SESSION_PATH } from "../src/lib/routes";

/** The SPA shell's browser chrome (NIGHTJAR #1531, docs/nightjar-ledger.md `web/index.html`): the
 *  `theme-color` meta is the theme's ground, tokens.css `--bg-0`, for a light operator as for a
 *  dark one. The inline boot script sets it before first paint (checked with the bundle held, so
 *  only index.html runs), and the app keeps it there once it has booted. Graded against the
 *  computed token, so a palette change can't leave the shell behind. */

const meta = (page: Page) => page.locator('meta[name="theme-color"]').getAttribute("content");
const ground = (page: Page) =>
  page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--bg-0").trim());

for (const theme of ["dark", "light", "system"] as const) {
  test.describe(`stored theme ${theme}`, () => {
    test.use({ colorScheme: theme === "system" ? "light" : "dark" });

    test.beforeEach(async ({ page }) => {
      await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    });

    test("pre-paint: the inline script points the chrome at the theme's ground", async ({ page }) => {
      // The module bundle never answers: what the page shows is index.html's own doing.
      await page.route(/\/(assets\/index-[^/]+\.js|src\/main\.tsx)(\?|$)/, () => {});
      await page.goto(NEW_SESSION_PATH, { waitUntil: "domcontentloaded" });
      const want = theme === "dark" ? "dark" : "light";
      await expect(page.locator("html")).toHaveAttribute("data-theme", want);
      expect((await meta(page))?.toLowerCase()).toBe(await ground(page));
    });

    test("booted: the app keeps the chrome on the theme's ground", async ({ page }) => {
      await page.goto(NEW_SESSION_PATH);
      await expect(page.locator(".hud-brand")).toBeVisible();
      expect((await meta(page))?.toLowerCase()).toBe(await ground(page));
    });
  });
}
