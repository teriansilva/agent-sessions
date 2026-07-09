import { expect, test } from "@playwright/test";

// #579 app-only connect: the public connect page no longer exposes the recovery terminal pane.
// Real browser guard because this is page structure/layout, not a jsdom-only contract.
test("Home Free connect page exposes the app root, not a recovery terminal pane", async ({ page }) => {
  await page.goto("/connect.html");

  await expect(page.getByRole("button", { name: "CONNECT" })).toBeVisible();
  await expect(page.locator("#app-root")).toBeVisible();
  await expect(page.locator("#term")).toHaveCount(0);
  await expect(page.locator(".xterm")).toHaveCount(0);
});
