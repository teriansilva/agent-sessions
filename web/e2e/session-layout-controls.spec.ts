import { expect, test, type Page } from "@playwright/test";
import { ID, mockStructuredGuidance } from "./structuredGuidanceFixture";
import { TOUCH_FLOOR } from "./touchTarget";

async function voice(page: Page) {
  await page.addInitScript(() => { window.SpeechRecognition = class {} as never; });
}
async function matchedButtons(page: Page) {
  const talk = page.getByRole("button", { name: /start voice input/i });
  const send = page.getByRole("button", { name: "Send", exact: true });
  await expect(talk).toHaveText("Talk");
  const t = (await talk.boundingBox())!, s = (await send.boundingBox())!;
  expect(t.height).toBe(s.height);
  expect(t.width).toBe(s.width);
  expect(t.y).toBe(s.y);
  expect(t.x + t.width).toBeLessThanOrEqual(s.x);
  if (await page.evaluate(() => matchMedia("(pointer: coarse), (max-width: 800px)").matches))
    expect(t.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(await talk.evaluate(el => getComputedStyle(el).font)).toBe(await send.evaluate(el => getComputedStyle(el).font));
  for (const button of [talk, send]) {
    await expect(button).toBeInViewport();
    const box = (await button.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  }
}

test("terminal composer matches Talk and Send at full and narrow widths", async ({ page }, info) => {
  await voice(page);
  await page.addInitScript(() => {
    window.WebSocket = class {
      readyState = 0;
      onopen?: () => void;
      onclose?: (e: { code: number }) => void;
      constructor() { setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 20); }
      send() {}
      close() { this.readyState = 3; this.onclose?.({ code: 1000 }); }
    } as never;
  });
  await page.goto("/s/claude/layout-controls-1478");
  await expect(page.locator(".xterm")).toBeVisible();
  if (await page.getByRole("button", { name: "Open compose box" }).isVisible())
    await page.getByRole("button", { name: "Open compose box" }).click();
  await page.getByPlaceholder(/Type here/).fill("Review the session layout.");
  await matchedButtons(page);
  await page.screenshot({ path: info.outputPath("terminal-controls-full.png") });
  await page.setViewportSize({ width: 320, height: 640 });
  await matchedButtons(page);
  await page.screenshot({ path: info.outputPath("terminal-controls.png") });
});

for (const theme of ["dark", "light"]) {
 test(`API composer keeps a small left toggle and matched Talk/Send — ${theme}`, async ({ page }, info) => {
  await voice(page);
  const fixture = await mockStructuredGuidance(page, "codex-api", "steer", theme);
  await page.goto(`/s/codex-api/${ID}`);
  await page.getByRole("textbox", { name: /^Message / }).fill("Review the session layout.");
  const toggle = page.getByTestId("structured-always-send-now");
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  await expect(toggle).toHaveText("Always send now");
  await expect(toggle.locator("svg")).toHaveCount(1);
  await expect(toggle).toHaveAttribute("title", /into the current turn/);
  // A plate paints its face and edge on pseudo-elements (nj-plate): compare those and the ink.
  const color = await toggle.evaluate(el => [getComputedStyle(el, "::before").backgroundColor, getComputedStyle(el, "::after").backgroundColor, getComputedStyle(el).color].join("|"));
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await expect(toggle).toHaveAttribute("title", /queue messages again/);
  await expect.poll(() => fixture.calls.length).toBe(1);
  expect(await toggle.evaluate(el => [getComputedStyle(el, "::before").backgroundColor, getComputedStyle(el, "::after").backgroundColor, getComputedStyle(el).color].join("|"))).not.toBe(color);
  await matchedButtons(page);
  const form = page.getByRole("form", { name: "Compose message" });
  // An intrinsic-width tool group must not count a nonexistent trailing gap as overflow.
  await expect(form.getByRole("button", { name: "Use a template", exact: true })).toBeVisible();
  await expect(form.getByRole("button", { name: "More keys", exact: true })).toHaveCount(0);
  const tools = form.getByRole("button", { name: "Attach files" });
  const a = (await tools.boundingBox())!, t = (await toggle.boundingBox())!;
  expect(t.y).toBe(a.y);
  expect(t.height).toBe(a.height);
  expect(t.x - a.x).toBeLessThan(180);
  await page.screenshot({ path: info.outputPath(`api-controls-${theme}-on.png`) });
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  // Desktop with a narrow pane exercises the container rule independently of media queries.
  if (info.project.name === "desktop")
    await page.locator(".terminal-pane").evaluate(el => { (el as HTMLElement).style.width = "360px"; });
  else await page.setViewportSize({ width: 320, height: 640 });
  await matchedButtons(page);
  await expect(toggle).toBeInViewport();
  // ONE ROW (operator, 2026-10-22): narrow, the toggle stays beside the tools, Talk and Send as its
  // icon — never a second line — and keeps its name for a screen reader.
  const box = (await toggle.boundingBox())!, send = (await form.getByRole("button", { name: "Send", exact: true }).boundingBox())!;
  expect(Math.abs(box.y + box.height / 2 - (send.y + send.height / 2))).toBeLessThan(2);
  expect(box.x + box.width).toBeLessThanOrEqual(send.x);
  expect(box.width).toBeLessThanOrEqual(48);
  await expect(toggle).toHaveAccessibleName("Always send now");
  // …and no control in the row is painted over another (KeyBar used to keep a key it had no room
  // for, which spilled under the toggle on a 320 px phone).
  const row = await form.locator("button:visible").evaluateAll(els =>
    els.map(e => e.getBoundingClientRect()).filter(r => r.width > 0 && r.top > 0).map(r => [r.left, r.right, r.top, r.bottom]));
  for (let i = 0; i < row.length; i++)
    for (let j = i + 1; j < row.length; j++) {
      const [l1, r1, t1, b1] = row[i], [l2, r2, t2, b2] = row[j];
      const overlapX = Math.min(r1, r2) - Math.max(l1, l2), overlapY = Math.min(b1, b2) - Math.max(t1, t2);
      expect(overlapX > 1 && overlapY > 1, `buttons ${i} and ${j} overlap`).toBe(false);
    }
  expect(await form.evaluate(el => el.scrollWidth - el.clientWidth)).toBe(0);
  await page.screenshot({ path: info.outputPath(`api-controls-${theme}.png`) });
 });
}

test("Missions comes before Sessions at desktop and mobile widths", async ({ page }) => {
  await mockStructuredGuidance(page, "codex-api", "steer");
  await page.goto(`/s/codex-api/${ID}`);
  const nav = page.getByTestId("section-nav");
  const order = async () => {
    await expect.poll(() => nav.locator(".section-nav-label").allTextContents())
      .toEqual(["Dashboard", "Missions", "Sessions", "Library"]);
    const m = (await nav.getByRole("link", { name: "Missions", exact: true }).boundingBox())!;
    const s = (await nav.getByRole("link", { name: "Sessions", exact: true }).boundingBox())!;
    expect(m.x + m.width).toBeLessThanOrEqual(s.x);
  };
  await order();
  await page.setViewportSize({ width: 320, height: 640 });
  await order();
});

// Hermes 6763: the real "starting" note ("Type now — Send unlocks when the agent is ready.") is
// 357 px of text; at 320 px it pushed Send off-screen once the row stopped wrapping. Narrow, the
// note now takes its own line above the buttons, and the buttons stay one row.
test("a populated status note never pushes Send off a 320 px composer (#1586)", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  const fixture = await mockStructuredGuidance(page, "codex-api", "steer");
  (fixture.state as Record<string, unknown>).start_in_flight = true;
  await page.goto(`/s/codex-api/${ID}`);
  const form = page.getByRole("form", { name: "Compose message" });
  await expect(form.getByRole("status").filter({ hasText: "Send unlocks" })).toHaveCount(1);
  // While the launch is in flight the send button reads "Starting…": find it by its test id.
  const send = form.getByTestId("structured-send");
  await expect(send).toBeInViewport({ ratio: 1 });
  const note = (await form.getByRole("status").filter({ hasText: "Send unlocks" }).boundingBox())!;
  const s = (await send.boundingBox())!;
  expect(note.y + note.height, "the note sits above the button row").toBeLessThanOrEqual(s.y + 1);
  const row = await form.locator("button:visible").evaluateAll((els) =>
    els.map((e) => e.getBoundingClientRect()).filter((r) => r.width > 0).map((r) => [r.left, r.right, r.top, r.bottom]));
  const centres = new Set(row.map(([, , t, b]) => Math.round((t + b) / 2)));
  expect(centres.size, "every button on one row").toBe(1);
  for (let i = 0; i < row.length; i++)
    for (let j = i + 1; j < row.length; j++) {
      const ox = Math.min(row[i][1], row[j][1]) - Math.max(row[i][0], row[j][0]);
      expect(ox > 1, `buttons ${i} and ${j} overlap`).toBe(false);
    }
  expect(await form.evaluate((el) => el.scrollWidth - el.clientWidth)).toBe(0);
});
