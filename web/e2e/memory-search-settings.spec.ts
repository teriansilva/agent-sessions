import { expect, test } from "@playwright/test";
import { commonMocks, CONFIG } from "./mission-directions";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

for (const theme of ["dark", "light"] as const) {
  test(`shared memory connection: draft test, save, and touch controls · ${theme}`, async ({ page, isMobile }, info) => {
    await commonMocks(page);
    await mockRoster(page);
    let saved = { base_url: "https://saved.example/v1", model: "old-embedding", api_key_set: true, needs_reentry: false, configured: true, dimensions: 384 };
    const writes: unknown[] = [];
    const tests: unknown[] = [];
    await page.route("**/api/memory-search/**", async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (request.method() !== "GET") expect(request.headers()["x-csrf-token"]).toBe(CONFIG.csrf);
      if (path.endsWith("/status")) return route.fulfill({ json: { state: "keyword-only", notes: 80 } });
      if (path.endsWith("/settings")) {
        if (request.method() === "GET") return route.fulfill({ json: saved });
        const body = request.postDataJSON();
        writes.push(body);
        saved = { ...saved, base_url: body.base_url, model: body.model };
        return route.fulfill({ json: saved });
      }
      tests.push(request.postDataJSON());
      return route.fulfill({ json: { ok: true, dimensions: 384, detail: "Draft connection verified. Save to use it." } });
    });
    await page.goto("/settings/ai-hq");
    await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
    const groups = page.getByRole("navigation", { name: "HQ settings groups" });
    await groups.getByRole("link", { name: "Memory search", exact: true }).click();
    await expect(page).toHaveURL(/\/settings\/ai-hq#memory-search-h$/);
    const section = page.getByTestId("memory-search-settings");
    const endpoint = page.getByLabel("Embeddings endpoint");
    const model = page.getByLabel("Embedding model", { exact: true });
    const key = section.getByLabel("API key (optional)");
    await expect(endpoint).toHaveValue(saved.base_url);
    await expect(key).toHaveValue("");
    await endpoint.fill("https://draft.example/v1");
    await key.fill("example-only-key");
    await model.fill("new-embedding");
    await groups.getByRole("link", { name: "HQ models", exact: true }).click();
    await groups.getByRole("link", { name: "Memory search", exact: true }).click();
    await expect(endpoint).toHaveValue("https://draft.example/v1");
    await expect(model).toHaveValue("new-embedding");
    await section.getByRole("button", { name: "Test draft connection" }).click();
    await expect(section.getByText("Draft connection verified. Save to use it.")).toBeVisible();
    expect(writes).toEqual([]);
    expect(saved.model).toBe("old-embedding");
    expect(tests).toEqual([{ base_url: "https://draft.example/v1", model: "new-embedding", api_key: "example-only-key" }]);
    await model.fill("newer-embedding");
    await expect(section.getByText(/Draft changed/)).toBeVisible();
    await expect(section.getByText("Draft connection verified. Save to use it.")).toHaveCount(0);
    await section.getByRole("button", { name: "Save connection" }).click();
    await expect(key).toHaveValue("");
    expect(writes).toEqual([{ base_url: "https://draft.example/v1", model: "newer-embedding", api_key: "example-only-key" }]);
    // NIGHTJAR fields use the touch floor on coarse pointers; desktop uses the shared field.
    const controls = [section.getByRole("button", { name: "Save connection" })];
    if (isMobile) controls.push(endpoint, model, key);
    for (const control of controls) {
      expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await section.screenshot({ path: info.outputPath(`memory-search-${theme}.png`) });
  });
}
