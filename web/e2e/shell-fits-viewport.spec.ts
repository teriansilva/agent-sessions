/** The shell fits the viewport with or without the list column.
 *
 *  A lone `1fr` track takes its minimum from its items, so a full footer widened the whole shell:
 *  with the list closed, and on every route with no list (Dashboard, Settings), the right of the
 *  top bar, the pane and the footer ran off screen. With the list open the same content fit. Layout,
 *  so a real browser. The hostname is what fills the footer here: an unstamped preview build shows
 *  no update chip, which is what filled it on the operator's screen.
 */
import { expect, test, type Page } from "@playwright/test";

import { CONFIG, commonMocks } from "./mission-directions";
import { DASHBOARD_PATH, NEW_SESSION_PATH } from "../src/lib/routes";

const VIEWPORT_HEIGHT = 760;

async function fullFooterMocks(page: Page) {
  await commonMocks(page);
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        ...CONFIG,
        hostname: "a-host-with-a-rather-long-name.internal.example.com",
        username: "admin",
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "0.0.11.dev+g16648df827a67" } }),
  );
  await page.route("**/api/telemetry", (r) =>
    r.fulfill({
      json: {
        cpu_pct: 4,
        mem: { pct: 22, used: 56.8 * 2 ** 30, total: 255 * 2 ** 30 },
        hq: {
          ctx_tokens: 6900,
          ctx_estimated: false,
          today_tokens: 62800,
          today_estimated: false,
        },
      },
    }),
  );
}

/** Everything that broke, measured once per width: the shell's bars and pane, the last control in
 *  the top bar, the host in the footer, and any two top-bar controls that run into each other. */
const shellFit = (page: Page) =>
  page.evaluate(() => {
    const right = (selector: string) =>
      document.querySelector(selector)?.getBoundingClientRect().right ?? null;
    const controls = [...document.querySelectorAll(".hud-topbar a, .hud-topbar button")]
      .map((el) => el.getBoundingClientRect())
      .filter((box) => box.width > 0);
    const overlaps = controls.filter(
      (box, i) => i > 0 && box.left < controls[i - 1].right - 0.5,
    ).length;
    return {
      vw: document.documentElement.clientWidth,
      rights: {
        app: right(".app"),
        topbar: right(".hud-topbar"),
        operator: right(".operator-slot"),
        pane: right(".terminal-pane"),
        footer: right(".hud-classbar"),
        host: right(".hud-footer-left"),
      },
      overlaps,
    };
  });

const CASES = [
  { name: "the list closed", path: NEW_SESSION_PATH, collapsed: "1" },
  { name: "the list open", path: NEW_SESSION_PATH, collapsed: "0" },
  { name: "no list (Dashboard)", path: DASHBOARD_PATH, collapsed: "0" },
];

for (const { name, path, collapsed } of CASES) {
  test(`the shell fits every width above the phone form with ${name}`, async ({ page }) => {
    test.setTimeout(120_000);
    await fullFooterMocks(page);
    await page.addInitScript(
      (value) => localStorage.setItem("tr-sidebar-collapsed", value),
      collapsed,
    );
    await page.setViewportSize({ width: 1400, height: VIEWPORT_HEIGHT });
    await page.goto(path);
    await expect(page.locator(".hud-version")).toBeVisible();
    await expect(page.locator('[data-testid="tm-cpu"]')).toHaveText("4%");

    // Every pixel, not sample widths: each band of the top bar starts wider than the last ended.
    const misfits: string[] = [];
    for (let width = 801; width <= 1400; width += 1) {
      await page.setViewportSize({ width, height: VIEWPORT_HEIGHT });
      const fit = await shellFit(page);
      const past = Object.entries(fit.rights)
        .filter(([, right]) => right === null || right > fit.vw + 0.5)
        .map(([part, right]) => `${part}=${right}`);
      if (past.length > 0 || fit.overlaps > 0) {
        misfits.push(`${width}: ${past.join(" ")} overlaps=${fit.overlaps}`);
      }
    }
    expect(misfits).toEqual([]);

    // Measured, then used: the corner tile answers at a width that overflowed.
    await page.setViewportSize({ width: 1180, height: VIEWPORT_HEIGHT });
    await page.locator(".operator-slot button").first().click();
    await expect(page.locator(".operator-slot button").first()).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });
}
