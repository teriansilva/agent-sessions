import { expect, test } from "@playwright/test";

// #500: the compose bar is a single row — one collapsible key group (↑ ↓ ↵ tab 📎 ✕) that overflows
// into ONE "…" menu when narrow, then the mic, then Send (always last). No second (kebab) menu; no
// esc / copy / interrupt chips. Real-browser test on desktop + mobile.

// WebSocket stub recording every frame the app sends (mirrors compose-empty-send.spec.ts).
const RECORDING_WS = `
window.__sent = [];
window.WebSocket = class {
  constructor(url) { this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 20);
  }
  send(d) { window.__sent.push(String(d)); }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

test("single-row bar: keys + attach + close in one group, no kebab/interrupt, Send last (#500)", async ({
  page,
}) => {
  await page.addInitScript(RECORDING_WS);
  await page.goto("/s/claude/compose-actions-500");
  await expect(page.locator(".xterm")).toBeVisible();
  await page.waitForFunction(
    () =>
      ((window as unknown as { __sent?: unknown[] }).__sent?.length ?? 0) > 0,
  );

  // Open the box if collapsed (desktop default) so Send + the close chip render.
  const send = page.getByRole("button", { name: /^send/i });
  if (!(await send.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(send).toBeVisible();

  // No second (kebab) menu, and no interrupt / Ctrl-C control anywhere.
  await expect(page.getByRole("button", { name: /more actions/i })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("button", { name: /interrupt|ctrl-c/i }),
  ).toHaveCount(0);

  // Send is the LAST control — everything (the key group + its overflow "…" + the mic) is to its
  // left. Up is always the first inline chip.
  const sendX = (await send.boundingBox())!.x;
  expect(
    (await page.getByRole("button", { name: "Up" }).boundingBox())!.x,
  ).toBeLessThan(sendX);

  // The group either fits inline, or its trailing chips collapse into the SINGLE "…" overflow when
  // narrow (the dynamic behavior). Either way attach + close are reachable, and Send stays last.
  const more = page.getByRole("button", { name: /more keys/i });
  if (await more.isVisible()) {
    expect((await more.boundingBox())!.x).toBeLessThan(sendX); // the "…" is left of Send too
    await more.click();
    await expect(
      page.getByRole("menuitem", { name: /attach file/i }),
    ).toBeVisible();
    await page.getByRole("menuitem", { name: /collapse compose box/i }).click();
  } else {
    const attach = page.getByRole("button", { name: /attach file/i });
    const close = page.getByRole("button", { name: /collapse compose box/i });
    expect((await attach.boundingBox())!.x).toBeLessThan(sendX);
    expect((await close.boundingBox())!.x).toBeLessThan(sendX);
    await close.click();
  }
  // Collapsing (inline chip or menu item) returns the compose/open affordance.
  await expect(
    page.getByRole("button", { name: /open compose box/i }),
  ).toBeVisible();
});

test("the overflow … menu is fully on-screen (not clipped) at a narrow width (#500)", async ({
  page,
}) => {
  await page.addInitScript(RECORDING_WS);
  // Force the key group to overflow into the "…" menu (Hermes: the in-tree popover was clipped by
  // .compose's overflow-y:auto / .terminal-pane's overflow:hidden on narrow widths).
  await page.setViewportSize({ width: 280, height: 640 });
  await page.goto("/s/claude/compose-narrow-500");
  await expect(page.locator(".xterm")).toBeVisible();
  await page.waitForFunction(
    () =>
      ((window as unknown as { __sent?: unknown[] }).__sent?.length ?? 0) > 0,
  );

  const send = page.getByRole("button", { name: /^send/i });
  if (!(await send.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(send).toBeVisible();

  // At 280px the group can't fit → the "…" trigger appears; open it.
  const more = page.getByRole("button", { name: /more keys/i });
  await expect(more).toBeVisible();
  await more.click();
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();

  // The popover is fully within the viewport on all four edges (portalled + clamped).
  const box = (await menu.boundingBox())!;
  const vw = page.viewportSize()!.width;
  const vh = page.viewportSize()!.height;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(vw + 1);
  expect(box.y + box.height).toBeLessThanOrEqual(vh + 1);

  // A collapsed-away action (the close chip) is reachable from the menu.
  await expect(
    page.getByRole("menuitem", { name: /collapse compose box/i }),
  ).toBeVisible();
});
