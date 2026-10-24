import { expect, test } from "@playwright/test";

/** The session pane's waiting and failed panels (RuntimeGate.module.css, NIGHTJAR #1531): the
 *  failed panel's LED paints the theme's LED halo (`--glow-led`: a 5px halo on dark, none on
 *  light, docs/design.md v2), never a fixed blur of its own. A browser that has never loaded the
 *  agent list, with the list never answering, shows "Opening…" and then gives up. */

// A never-visited browser: no stored runtime hints (the config seeds a returning one).
test.use({ storageState: { cookies: [], origins: [] } });

for (const theme of ["dark", "light"] as const) {
  test(`the agent-list-unavailable LED carries the ${theme} theme's halo`, async ({ page }) => {
    test.setTimeout(30_000);
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await page.route(/\/api\/engines(\?|$)/, () => {}); // never answers
    await page.goto("/s/claude/7c1e6a00-5b2d-4c8e-9f31-0a6d2e8b4006");
    await expect(page.locator("[data-session-opening='opening']")).toBeVisible();
    const failed = page.locator("[data-session-opening='failed']");
    await expect(failed).toBeVisible({ timeout: 15_000 });
    const { shadow, halo } = await failed.locator("span[aria-hidden='true']").first().evaluate((el) => ({
      shadow: getComputedStyle(el).boxShadow,
      halo: getComputedStyle(document.documentElement).getPropertyValue("--glow-led").trim(),
    }));
    // box-shadow computes to "<colour> <x> <y> <blur> <spread>": the blur is the theme's LED halo.
    const lengths = shadow.match(/-?[\d.]+px/g) ?? [];
    expect(lengths, `computed box-shadow ${shadow}`).toHaveLength(4);
    expect(lengths[2]).toBe(halo);
  });
}
