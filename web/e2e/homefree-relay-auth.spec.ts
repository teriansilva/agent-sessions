/** #1735: the box's own server pages (`/login`, `/change-password`) are not reachable through the
 *  relay. A box that refuses the streamed app — "sign in", or a first-run password change — must
 *  end the relay session with that reason on the connect page, never navigate to a page the
 *  landing host can't serve, and never loop. Both refusals land DURING the app's mount (its first
 *  /api/config), before the connection is published: the hard case. Driven with the real SPA in
 *  the real connect page; the harness stands in for the tunnel only (relayHarness.ts). */
import { expect, test, type Page } from "@playwright/test";

import { connectRelayed, RELAY_HOST_LAUNCH } from "./relayHarness";

test.use(RELAY_HOST_LAUNCH);

/** Document loads of the main frame (history updates inside the page are not loads). */
async function navigations(page: Page): Promise<() => string[]> {
  const urls: string[] = [];
  page.on("request", (r) => {
    if (r.isNavigationRequest() && r.frame() === page.mainFrame()) urls.push(new URL(r.url()).pathname);
  });
  return () => urls;
}

for (const c of [
  {
    name: "a 401 (the box wants a sign-in)",
    api: (page: Page) => page.route("**/api/config", (r) => r.fulfill({ status: 401, json: { detail: "unauthorized" } })),
    reason: "the box asked you to sign in",
  },
  {
    name: "a required first-run password change",
    api: (page: Page) =>
      page.route("**/api/config", (r) =>
        r.fulfill({ json: { csrf: "x", auth_mode: "password", must_change_password: true, terminal_backend: "ws" } }),
      ),
    reason: "the box needs its password changed first",
  },
]) {
  test(`${c.name} ends the relay session with its reason, on the connect page, with no navigation`, async ({
    page,
    baseURL,
  }) => {
    const seen = await navigations(page);
    // Hold the attempt in its mount (the harness's seam): the app is rendered and asks the box,
    // but nothing is published yet, so the refusal reaches the shell only through `armRelay`.
    await page.addInitScript(() => {
      (window as unknown as { __holdMount: Promise<void> }).__holdMount = new Promise(() => {});
    });
    await connectRelayed(page, baseURL, { api: c.api, expectConnected: false });
    await expect(page.locator("#status")).toContainText(c.reason, { timeout: 20_000 });
    await expect(page.locator("#status")).toHaveAttribute("data-kind", "error");
    await expect(page.locator("body")).not.toHaveAttribute("data-state", "connected");
    await expect(page.getByTestId("relay-plate")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Connect" })).toBeVisible();
    // Only the connect page's own load: no /login or /change-password document, no loop.
    await page.waitForTimeout(1000);
    expect(seen().filter((p) => /login|change-password/.test(p))).toEqual([]);
    expect(seen()).toHaveLength(1);
  });
}
