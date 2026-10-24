/** Settings → AI → HQ calls, P3 and P4 (#1397), in a real browser on desktop and mobile.
 *
 *  - Each General's voice: all 13 rows reachable by scrolling, no sideways scroll, every control
 *    at least 44 px on a phone, controls named for their General, focus kept across a save (the
 *    row is never remounted), the voice-per-language editor with a Missing voice.
 *  - Speech on this machine: Copy puts the LITERAL command on the clipboard at 320 px wide, where
 *    the command wraps; Use this machine saves server-side and shows the check. */
import { expect, test, type Page } from "@playwright/test";

import { SETTINGS, mockShell } from "./callFixture";
import { TOUCH_FLOOR } from "./touchTarget";

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

const PHONE = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;

async function noSideways(page: Page) {
  const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(over).toBeLessThanOrEqual(0);
}

test("each General's voice: 13 rows, named controls, 44 px on a phone, focus kept across a save", async ({ page }) => {
  const srv = await mockShell(page);
  srv.settings = {
    ...SETTINGS,
    tts_styles: ["calm", "serious", "dramatic"],
    tts_voices: ["aria", "atlas", "kerstin"],
    tts_languages: ["en", "de", "fr"],
    contacts: { "russia-man": { language_voices: { de: "kerstin", fr: "margot" } } },
  };
  await page.goto("/settings/ai-hq-calls");
  const rows = page.getByTestId("hq-calls-contact");
  await expect(rows).toHaveCount(13);
  for (let i = 0; i < 13; i++) {
    const row = rows.nth(i);
    await row.scrollIntoViewIfNeeded();
    await expect(row).toBeInViewport();
    const name = (await row.locator("b").first().textContent())!;
    await expect(row.getByRole("button", { name: `Preview ${name}` })).toBeVisible();
    await expect(row.getByRole("button", { name: `Voices per language for ${name}` })).toBeVisible();
    await expect(row.getByRole("combobox", { name: `Style for ${name}` })).toBeVisible();
    if (PHONE(page)) {
      for (const el of await row.locator("button:visible, input:visible, select:visible").all()) {
        const box = (await el.boundingBox())!;
        expect(box.height, `${name}: ${await el.getAttribute("aria-label")}`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
        expect(box.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      }
    }
  }
  await noSideways(page);

  // A save keeps focus where it is: the row is updated in place, never remounted.
  const speed = page.getByRole("combobox", { name: "Speed for General Jessica Hale" });
  await speed.focus();
  await speed.selectOption("1.05");
  await expect.poll(() => srv.saves.length).toBe(1);
  expect(srv.saves[0]).toEqual({ contacts: { "us-woman": { speed: 1.05 } } });
  await expect(speed).toBeFocused();
  // Keyboard order within a row: the voice, then style, speed, Preview, Languages.
  await page.getByRole("combobox", { name: "Voice for General Hana Seo" }).focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("combobox", { name: "Style for General Hana Seo" })).toBeFocused();
});

test("voices per language: the editor opens in place, a missing voice is named, remove saves", async ({ page }) => {
  const srv = await mockShell(page);
  srv.settings = {
    ...SETTINGS,
    tts_voices: ["atlas", "kerstin"],
    tts_languages: ["en", "de", "fr"],
    contacts: { "russia-man": { language_voices: { de: "kerstin", fr: "margot" } } },
  };
  await page.goto("/settings/ai-hq-calls");
  const toggle = page.getByRole("button", { name: "Voices per language for General Dmitri Sokolov" });
  await toggle.scrollIntoViewIfNeeded();
  await expect(toggle).toHaveText(/Languages · 2/);
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(toggle).toBeFocused();
  const editor = page.locator(`#${await toggle.getAttribute("aria-controls")}`);
  await expect(editor).toBeVisible();
  await expect(editor.getByTestId("hq-calls-lv-missing")).toHaveText("Missing");
  // The stamp sits on its own line under the voice field, never floating beside the buttons.
  const fr = editor.locator('[data-testid="hq-calls-lv-row"][data-lang="fr"]');
  const field = (await fr.getByRole("combobox", { name: "French voice for General Dmitri Sokolov" }).boundingBox())!;
  const stamp = (await fr.getByTestId("hq-calls-lv-missing").boundingBox())!;
  expect(stamp.y).toBeGreaterThanOrEqual(field.y + field.height - 1);
  // Each language row's buttons sit on its field's line.
  for (const lang of ["de", "fr"]) {
    const r = editor.locator(`[data-testid="hq-calls-lv-row"][data-lang="${lang}"]`);
    const input = (await r.locator("input").boundingBox())!;
    for (const btn of await r.getByRole("button").all()) {
      const box = (await btn.boundingBox())!;
      expect(Math.abs(box.y + box.height / 2 - (input.y + input.height / 2)), `${lang}: ${await btn.getAttribute("aria-label")}`).toBeLessThan(4);
    }
  }
  await expect(editor).toContainText("“margot” is no longer on your server: on French calls General Sokolov");
  await expect(page.getByRole("button", { name: "Preview General Dmitri Sokolov in German" })).toBeVisible();
  const remove = page.getByRole("button", { name: "Remove the French voice for General Dmitri Sokolov" });
  if (PHONE(page)) {
    const box = (await remove.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await remove.click();
  await expect.poll(() => srv.saves.length).toBe(1);
  expect(srv.saves[0]).toEqual({ contacts: { "russia-man": { language_voices: { de: "kerstin" } } } });
  await noSideways(page);
});

/** Text a control can't show whole: a name cut by ellipsis, a select whose chosen label is wider
 *  than its text box (a select's scrollWidth never grows, so its label is measured). */
async function clipped(page: Page, row: ReturnType<Page["locator"]>): Promise<string[]> {
  return await row.evaluate((el) => {
    const out: string[] = [];
    const name = el.querySelector("b") as HTMLElement;
    if (name.scrollWidth > name.clientWidth) out.push(`name: ${name.textContent}`);
    const ctx = document.createElement("canvas").getContext("2d")!;
    for (const sel of Array.from(el.querySelectorAll("select"))) {
      const cs = getComputedStyle(sel);
      ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
      const text = sel.selectedOptions[0]?.text ?? "";
      const room = sel.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
      if (ctx.measureText(text).width > room) out.push(`select: ${text}`);
    }
    return out;
  });
}

test("custom rows read whole on a desktop: the General's name and the select labels aren't clipped", async ({ page }, info) => {
  test.skip(info.project.name === "mobile", "the desktop row layout");
  const srv = await mockShell(page);
  srv.settings = {
    ...SETTINGS,
    tts_styles: ["calm", "serious", "dramatic", "energetic", "narrator", "neutral"],
    tts_voices: ["aria", "atlas", "nova", "kerstin"],
    contacts: {
      "russia-man": { language_voices: { de: "kerstin" } },
      "us-woman": { voice: "nova", speed: 1.05 },
      "southamerica-woman": { style: "energetic", speed: 1.05 },
    },
  };
  await page.goto("/settings/ai-hq-calls");
  const custom = page.locator('[data-testid="hq-calls-contact"][data-custom="true"]');
  await expect(custom).toHaveCount(3);
  for (const row of await custom.all()) {
    await row.scrollIntoViewIfNeeded();
    await expect(row.getByRole("button", { name: /^Reset / })).toBeVisible();
    expect(await clipped(page, row)).toEqual([]);
  }
  // Untouched rows too.
  for (const row of await page.locator('[data-testid="hq-calls-contact"][data-custom="false"]').all()) {
    expect(await clipped(page, row)).toEqual([]);
  }
});

test("speech on this machine: Copy puts the literal command on the clipboard at 320 px", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await mockShell(page);
  await page.goto("/settings/ai-hq-calls");
  const block = page.getByTestId("hq-calls-local");
  await expect(block).toHaveAttribute("data-state", "not_set_up");
  await expect(block).toContainText("Downloads about 700 MB after you confirm.");
  const copy = page.getByRole("button", { name: "Copy the setup command" });
  await copy.click();
  await expect(copy).toHaveText("Copied");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("agent-sessions voice setup");
  const box = (await copy.boundingBox())!;
  expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await noSideways(page);
});

test("stopped without a user service at 320 px: the serve command is copied whole", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  const srv = await mockShell(page);
  srv.local = { ...srv.local, installed: { runtime: true, server: true }, token: true, running: false, systemd: false };
  await page.goto("/settings/ai-hq-calls");
  await expect(page.getByTestId("hq-calls-local")).toHaveAttribute("data-state", "stopped");
  await page.getByRole("button", { name: "Copy the start command" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("agent-sessions voice serve");
});

test("speech on this machine: Use this machine saves server-side and shows the check; soft limits say so", async ({ page }) => {
  const srv = await mockShell(page);
  srv.local = { ...srv.local, installed: { runtime: true, server: true }, token: true, running: true, limits: "soft" };
  let used = 0;
  await page.route("**/api/voice/local/use", (r) => {
    used += 1;
    expect(r.request().postData() ?? "").not.toMatch(/token|key/i);
    return r.fulfill({
      json: {
        settings: {
          ...SETTINGS,
          stt: { base_url: "http://127.0.0.1:8766/v1", model: "faster-whisper", api_key_set: true },
          tts: { base_url: "http://127.0.0.1:8766/v1", model: "kokoro-v1.0", api_key_set: true },
          voices: { woman: "af_heart", man: "am_michael" },
        },
        report: { usable: true, checks: [{ id: "tts.synthesize", ok: true, required: true, note: "", value: null }] },
      },
    });
  });
  await page.goto("/settings/ai-hq-calls");
  const block = page.getByTestId("hq-calls-local");
  await expect(block).toHaveAttribute("data-state", "running");
  await expect(page.getByTestId("hq-calls-local-soft")).toHaveText(
    "Soft limits only: no memory or CPU cap without the user service.",
  );
  const use = page.getByTestId("hq-calls-local-use");
  if (PHONE(page)) expect((await use.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await use.click();
  await expect(page.getByTestId("hq-calls-local-report")).toHaveAttribute("data-usable", "true");
  expect(used).toBe(1);
  // The endpoints above now show this machine; the key reads Saved, never its value.
  await expect(page.getByTestId("hq-calls-stt-url")).toHaveValue("http://127.0.0.1:8766/v1");
  await expect(page.getByTestId("hq-calls-tts-token-state")).toHaveText("Saved");
  await noSideways(page);
});
