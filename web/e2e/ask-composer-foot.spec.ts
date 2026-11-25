/** The composer foot at sidebar width (#1775) — the hint must never paint over the plates.
 *
 *  What a DOM emulator cannot prove: actual box overlap and horizontal overflow at the widths
 *  the HQ sidebar really runs at. The composer's hint was `flex: none` + nowrap in a lead that
 *  may shrink below its content, so at ~460px it painted over Attach/Camera/Send. Pinned here
 *  on both projects: the hint's box never intersects a control's box, the row never overflows
 *  the panel, and the full hint still shows where there is room for it.
 */
import { expect, test } from "@playwright/test";

import { openAsk } from "./askSidebar";

function intersects(
  a: { x: number; y: number; width: number; height: number },
  b: { x: number; y: number; width: number; height: number },
): boolean {
  return (
    a.x < b.x + b.width &&
    b.x < a.x + a.width &&
    a.y < b.y + b.height &&
    b.y < a.y + a.height
  );
}

async function mockShell(page: import("@playwright/test").Page) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({
      json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } },
    }),
  );
  await page.route("**/api/ask/**", (r) => {
    if (r.request().url().includes("/conversations") && !r.request().url().includes("attachments"))
      return r.fulfill({
        json: {
          id: "ask_" + "a".repeat(32),
          title: "",
          turns: [],
          running: false,
          updates: [],
          following: [],
          conversations: [],
          configured: true,
        },
      });
    return r.fulfill({ json: {} });
  });
}

for (const width of [390, 460]) {
  test(`the hint never covers the plates at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await mockShell(page);
    await page.goto("/");
    const panel = await openAsk(page);
    const boxes = [];
    for (const id of ["ask-attach", "ask-camera", "composer-send"]) {
      const el = panel.getByTestId(id);
      await expect(el).toBeVisible();
      boxes.push((await el.boundingBox())!);
    }
    // Whatever part of the hint is shown truncates rather than overlapping a plate.
    const hint = panel.getByTestId("composer-hint");
    if (await hint.isVisible()) {
      const hb = (await hint.boundingBox())!;
      for (const b of boxes) expect(intersects(hb, b)).toBe(false);
    }
    // and the foot row never overflows the panel
    const overflow = await panel.evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
}

test("on a wide desktop the sidebar hint shows what fits and nothing overlaps", async ({
  page,
}) => {
  // #1775 rescoped: the composer's ONLY production mount is the HQ sidebar (≤440px), so the
  // "wide desktop" case is the sidebar at a desktop viewport — the box that actually occurs.
  await page.setViewportSize({ width: 1280, height: 900 });
  await mockShell(page);
  await page.goto("/");
  const panel = await openAsk(page);
  const hint = panel.getByTestId("composer-hint");
  await expect(hint).toBeVisible();
  // RENDERED text (innerText), not DOM text: the tail span is hidden by the container query
  // and must not be counted as shown.
  await expect(hint).toContainText("Enter to send");
  expect((await hint.innerText()).includes("Shift + Enter")).toBe(false);
  const hb = (await hint.boundingBox())!;
  for (const id of ["ask-attach", "ask-camera", "composer-send"]) {
    const b = (await panel.getByTestId(id).boundingBox())!;
    expect(intersects(hb, b)).toBe(false);
  }
  const overflow = await panel.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
