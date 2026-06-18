import { expect, test } from "@playwright/test";

// #494: the file-upload (paperclip) is promoted to the compose row next to Send; the interrupt /
// "stop" (Ctrl-C) leaves the inline key row and lives in a ⋮ kebab menu that opens DOWNWARD; the
// × collapse toggle stays. Real-browser test — box geometry, a portalled menu, and the PTY frame.

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

const CTRLC_FRAME = '"d":"\\u0003"'; // {"t":"i","d":"\x03"} — Ctrl-C, JSON-escaped

test("paperclip is promoted next to Send; interrupt only in the ⋮ menu, opening downward (#494)", async ({
  page,
}) => {
  await page.addInitScript(RECORDING_WS);
  await page.goto("/s/claude/compose-actions-494");
  await expect(page.locator(".xterm")).toBeVisible();
  await page.waitForFunction(
    () => ((window as unknown as { __sent?: unknown[] }).__sent?.length ?? 0) > 0,
  );

  // The box is collapsed by default on desktop; open it so Send renders.
  const send = page.getByRole("button", { name: /^send/i });
  if (!(await send.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(send).toBeVisible();

  // Attach (file upload) sits in the right cluster, to the RIGHT of Send.
  const attach = page.getByRole("button", { name: /attach file/i });
  await expect(attach).toBeVisible();
  expect((await attach.boundingBox())!.x).toBeGreaterThan((await send.boundingBox())!.x);

  // Interrupt is NOT an inline chip: nothing exposes "interrupt" until the kebab is opened.
  await expect(page.getByRole("button", { name: /interrupt/i })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: /interrupt/i })).toHaveCount(0);

  // Open the kebab → the menu opens BELOW it (downward), holding the interrupt item.
  const kebab = page.getByRole("button", { name: /more actions/i });
  const kebabBox = (await kebab.boundingBox())!;
  await kebab.click();
  const item = page.getByRole("menuitem", { name: /interrupt/i });
  await expect(item).toBeVisible();
  expect((await item.boundingBox())!.y).toBeGreaterThan(kebabBox.y + kebabBox.height - 2);

  // Activating it sends a Ctrl-C frame to the PTY.
  await item.click();
  await page.waitForFunction(
    (m) => ((window as unknown as { __sent?: string[] }).__sent ?? []).some((f) => f.includes(m)),
    CTRLC_FRAME,
  );

  // …and the menu closes after the action.
  await expect(item).toBeHidden();
});
