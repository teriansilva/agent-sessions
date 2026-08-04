import { expect, test, type Page } from "@playwright/test";

// #507: real-browser proof for the resizable desktop sidebar — drag the handle to change the
// `--sidebar-w` grid track, the pane reflows (which drives the xterm ResizeObserver), the width
// persists across reload, and double-click resets. On mobile the handle is not rendered.

async function setup(page: Page): Promise<void> {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
      },
    }),
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
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.goto("/");
}

/** The resolved `--sidebar-w` custom property on `.app`, in px. */
function sidebarW(page: Page): Promise<number> {
  return page
    .locator(".app")
    .evaluate((el) =>
      parseFloat(getComputedStyle(el).getPropertyValue("--sidebar-w")),
    );
}

test.describe("resizable sidebar (#507)", () => {
  test("drag resizes the column + reflows the pane, persists across reload, double-click resets", async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name !== "desktop",
      "the resize handle is desktop-only",
    );
    await setup(page);

    const handle = page.locator(".sidebar-resize");
    await expect(handle).toBeVisible();
    await expect(handle).toHaveAttribute("role", "separator");

    expect(await sidebarW(page)).toBe(320);
    const paneBefore = (await page.locator("main.terminal-pane").boundingBox())!
      .width;

    // Drag the handle 120px to the right.
    const box = (await handle.boundingBox())!;
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;
    await page.mouse.move(cx, cy);
    await page.mouse.down();
    await page.mouse.move(cx + 120, cy, { steps: 12 });
    await page.mouse.up();

    // The column grew by ~120px…
    const wAfter = await sidebarW(page);
    expect(wAfter).toBeGreaterThan(430);
    expect(wAfter).toBeLessThan(450);
    // …and the pane shrank to match (the reflow that fires Terminal.tsx's ResizeObserver).
    const paneAfter = (await page.locator("main.terminal-pane").boundingBox())!
      .width;
    expect(paneAfter).toBeLessThan(paneBefore - 100);

    // Width persists across a reload (device-local localStorage).
    await page.reload();
    await expect(page.locator(".sidebar-resize")).toBeVisible();
    expect(await sidebarW(page)).toBe(wAfter);

    // Double-click the handle resets to the 320px default.
    await page.locator(".sidebar-resize").dblclick();
    expect(await sidebarW(page)).toBe(320);
  });

  test("keyboard: arrow keys nudge the focused separator", async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name !== "desktop",
      "the resize handle is desktop-only",
    );
    await setup(page);
    const handle = page.locator(".sidebar-resize");
    await handle.focus();
    expect(await sidebarW(page)).toBe(320);
    await handle.press("ArrowRight");
    expect(await sidebarW(page)).toBe(336); // +WIDTH_STEP
    await handle.press("ArrowLeft");
    await handle.press("ArrowLeft");
    expect(await sidebarW(page)).toBe(304); // 336 − 16 − 16
  });

  test("no resize handle on mobile (the drawer stays fixed-width)", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "asserts the mobile absence");
    await setup(page);
    // Open the drawer so the sidebar is mounted; the handle must still be absent.
    await page.locator("header .navToggle").click();
    await expect(page.locator("aside.sidebar")).toBeVisible();
    await expect(page.locator(".sidebar-resize")).toHaveCount(0);
  });
});
