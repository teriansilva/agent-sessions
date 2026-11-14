/** Settings → AI → HQ → Question routing (#2028), in a real browser on desktop and mobile: the
 *  toggle, model and thinking controls are reachable, at least 44 px on a phone, never scroll the
 *  page sideways, and save exactly the field changed. Screenshots in both themes go to the test's
 *  output dir (the PR attaches them). */
import { expect, test, type Page } from "@playwright/test";

import { mockShell } from "./callFixture";
import { TOUCH_FLOOR } from "./touchTarget";

const PHONE = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;

async function routeSettings(page: Page) {
  const saves: unknown[] = [];
  let route = { enabled: true, model: "", thinking: "off" };
  await mockShell(page);
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "csrf-x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
        hq_contact: "asia-woman",
        hq_contact_rev: 0,
        hq_reports: false,
        hq_route: route,
      },
    }),
  );
  await page.route("**/api/ai-review/models**", (r) =>
    r.fulfill({ json: { models: ["anthropic-claude-server", "deepseek-v4.1-flash"] } }),
  );
  await page.route("**/api/prefs", async (r) => {
    const body = r.request().postDataJSON() as { hq_route: Record<string, unknown> };
    saves.push(body);
    route = { ...route, ...body.hq_route } as typeof route;
    await r.fulfill({ json: { hq_route: route } });
  });
  return saves;
}

for (const theme of ["dark", "light"] as const) {
  test(`question routing: controls, touch size, no sideways scroll, saves · ${theme}`, async ({ page }, info) => {
    const saves = await routeSettings(page);
    await page.goto("/settings/ai-hq");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    const toggle = page.getByTestId("hq-route-toggle");
    const model = page.getByLabel("Model for this decision");
    const thinking = page.getByLabel("Thinking for this decision");
    await expect(toggle).toBeChecked();
    await expect(model).toHaveValue("");
    await expect(page.getByRole("option", { name: "deepseek-v4.1-flash" })).toBeAttached();
    if (PHONE(page)) {
      for (const el of [model, thinking]) {
        expect((await el.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      }
    }
    const over = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(over).toBeLessThanOrEqual(0);
    await page.getByRole("region", { name: "Question routing" }).or(page.locator("section[aria-labelledby=hq-route-h]"))
      .first()
      .screenshot({ path: info.outputPath(`hq-route-${theme}.png`) });

    await model.selectOption("deepseek-v4.1-flash");
    await expect.poll(() => saves.length).toBe(1);
    expect(saves[0]).toEqual({ hq_route: { model: "deepseek-v4.1-flash" } });
    await toggle.click();
    await expect.poll(() => saves.length).toBe(2);
    expect(saves[1]).toEqual({ hq_route: { enabled: false } });
    await expect(model).toBeDisabled();
  });
}
