import { expect, test, type Locator } from "@playwright/test";
import { mockShell } from "./callFixture";

/** WCAG contrast of a text element against the first opaque background behind it. Colours are
 *  resolved by painting them, so `color-mix()` and modern colour syntax need no parser. */
async function contrast(el: Locator): Promise<number> {
  return el.evaluate((node) => {
    const ctx = document.createElement("canvas").getContext("2d")!;
    const rgba = (css: string) => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = "#000";
      ctx.fillStyle = css;
      ctx.fillRect(0, 0, 1, 1);
      return Array.from(ctx.getImageData(0, 0, 1, 1).data);
    };
    let bgNode: Element | null = node;
    let bg = [0, 0, 0, 0];
    while (bgNode) {
      bg = rgba(getComputedStyle(bgNode).backgroundColor);
      if (bg[3] === 255) break;
      bgNode = bgNode.parentElement;
    }
    const fg = rgba(getComputedStyle(node).color);
    const lum = ([r, g, b]: number[]) =>
      [r, g, b]
        .map((v) => v / 255)
        .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
        .reduce((acc, c, i) => acc + c * [0.2126, 0.7152, 0.0722][i], 0);
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  });
}

for (const theme of ["dark", "light"]) {
  test(`HQ model labels and source captions meet AA · ${theme}`, async ({ page }) => {
    await mockShell(page);
    await page.route("**/api/config", (r) => r.fulfill({ json: {
      csrf: "csrf-x", auth_mode: "none", onboarded: true, hq_contact: "off",
      ai_review: { model: "endpoint", thinking: "medium" },
      hq_route: { enabled: true, model: "", thinking: "off" },
      hq_models: { typed: { model: "", thinking: "hq" }, mission: { model: "", thinking: "hq" } },
    } }));
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await page.goto("/settings/ai-hq#hq-models-h");
    const group = page.getByRole("region", { name: "HQ models", exact: true });
    await expect(group.getByLabel("Model for typed turns")).toBeEnabled();
    for (const text of await group.locator("fieldset label, fieldset select + p").all()) {
      expect(await contrast(text), await text.textContent()).toBeGreaterThanOrEqual(4.5);
    }
  });
}
