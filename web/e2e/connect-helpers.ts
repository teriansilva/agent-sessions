import { expect } from "@playwright/test";

/**
 * Complete the connect page's human-verification hold (#690) so a test can reach the
 * Connect button. Uses a real pointer press-and-hold; the page's test harness shortens
 * the hold via `__battlelabConnectHarness.holdMs`.
 */
export async function verifyHuman(
  page: import("@playwright/test").Page,
): Promise<void> {
  const hold = page.locator("#gate-hold");
  await hold.hover();
  await page.mouse.down();
  await expect(page.locator("#verify-gate")).toHaveAttribute(
    "data-state",
    "verified",
    {
      timeout: 5_000,
    },
  );
  await page.mouse.up();
}
