import { expect, type Page } from "@playwright/test";

/** Use the shell's HQ menu, so a Missions round trip exercises client-side navigation. */
export async function openMissionsView(page: Page) {
  await page.locator(".hud-topbar").getByRole("button", { name: "HQ menu" }).click();
  const link = page.getByRole("menu", { name: "HQ menu" }).getByRole("menuitem", { name: "Missions", exact: true });
  await expect(link).toHaveAttribute("href", "/mission");
  await link.click();
}
