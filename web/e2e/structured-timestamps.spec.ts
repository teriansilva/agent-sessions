import { expect, test } from "@playwright/test";
import { COMMAND, ID, clients, mockStructuredGuidance } from "./structuredGuidanceFixture";

test.use({ locale: "en-GB", timezoneId: "Europe/Bucharest" });
const FIRST = "2026-10-12T07:30:42.000Z";

for (const [engine, mode] of clients) {
  for (const theme of ["dark", "light"]) {
    test(`${engine}: response time stays local and stable (${theme})`, async ({ page }, testInfo) => {
      if (testInfo.project.name === "mobile") await page.setViewportSize({ width: 360, height: 780 });
      const { state } = await mockStructuredGuidance(page, engine, mode, theme);
      state.turns[0].reply_ts = Date.parse(FIRST) / 1000;
      await page.goto(`/s/${engine}/${ID}`);
      const reply = page.getByTestId("structured-reply");
      const stamp = reply.locator("time");
      await expect(stamp).toHaveAttribute("datetime", FIRST);
      await expect(stamp).toHaveText("12 Oct 2026, 10:30");
      await expect(stamp).toHaveAttribute("aria-label", /Response started:.*12 October 2026.*10:30:42.*(EEST|GMT\+3)/);
      await expect(stamp).toHaveAttribute("title", await stamp.getAttribute("aria-label") as string);
      const box = (await stamp.boundingBox())!;
      const panel = (await reply.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(panel.x);
      expect(box.x + box.width).toBeLessThanOrEqual(panel.x + panel.width);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
      await page.screenshot({ path: testInfo.outputPath("timestamp.png") });

      await page.getByTestId("structured-activity").locator("summary").click();
      state.turns[0].reply += " Additional detail arrived.";
      state.revision++;
      await expect(reply).toContainText("Additional detail arrived.");
      await expect(stamp).toHaveAttribute("datetime", FIRST);
      await expect(page.getByText(COMMAND, { exact: true })).toBeVisible();
      await page.reload();
      await expect(stamp).toHaveText("12 Oct 2026, 10:30");
      await expect(stamp).toHaveAttribute("datetime", FIRST);
    });
  }

  test(`${engine}: unknown or invalid response times do not invent a date`, async ({ page }) => {
    const { state } = await mockStructuredGuidance(page, engine, mode);
    const template = state.turns[0];
    state.state = "completed";
    state.active_turn = "";
    state.turns = [undefined, null, "today", -1, 9e15].map((reply_ts, i) => ({
      ...template, turn_id: `unknown-${i}`, operation_id: `unknown-${i}`, state: "completed",
      text: "Question", reply: `Response with unknown time ${i}`, tools: [], reply_ts,
    }));
    await page.goto(`/s/${engine}/${ID}`);
    await expect(page.getByTestId("structured-reply")).toHaveCount(5);
    await expect(page.getByTestId("structured-reply").locator("time")).toHaveCount(0);
  });
}

test.describe("local date across midnight", () => {
  test.use({ timezoneId: "America/Los_Angeles" });
  for (const [engine, mode] of clients) {
    test(`${engine}: response date follows the browser timezone`, async ({ page }) => {
      const { state } = await mockStructuredGuidance(page, engine, mode);
      state.turns[0].reply_ts = Date.parse("2026-10-12T00:30:42Z") / 1000;
      await page.goto(`/s/${engine}/${ID}`);
      const stamp = page.getByTestId("structured-reply").locator("time");
      await expect(stamp).toHaveText("11 Oct 2026, 17:30");
      await expect(stamp).toHaveAttribute("datetime", "2026-10-12T00:30:42.000Z");
    });
  }
});
