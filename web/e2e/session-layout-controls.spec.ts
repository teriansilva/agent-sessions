import { expect, test, type Page } from "@playwright/test";
import { ID, mockStructuredGuidance } from "./structuredGuidanceFixture";

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
    expect(t.height).toBeGreaterThanOrEqual(44);
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
  const color = await toggle.evaluate(el => getComputedStyle(el).backgroundColor);
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await expect(toggle).toHaveAttribute("title", /queue messages again/);
  await expect.poll(() => fixture.calls.length).toBe(1);
  expect(await toggle.evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe(color);
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
  const box = (await toggle.boundingBox())!, send = (await form.getByRole("button", { name: "Send", exact: true }).boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(send.y);
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
