import { expect, test } from "@playwright/test";

import { connect, PY_STACK_AVAILABLE, startStack, type Stack } from "./appmode";

/** #1658 over the REAL path: the connect page, the real handshake and mux, the real agent and app.
 *  `homefree-relay-plate.spec.ts` covers the plate's states with a harness standing in for the
 *  tunnel; this proves the seam it relies on — the shell and the streamed SPA sharing one
 *  `relayStatus` module — holds in the real dynamic-import mount: the plate docks in the app's top
 *  bar, the shell's floating bar stands down, and the plate's Sign out ends the real tunnel. */
test.describe.configure({ mode: "serial" });
test.setTimeout(180_000);
test.skip(({ isMobile }) => !!isMobile, "transport proof — runs once, on desktop");
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv) — see .forgejo/workflows/appmode-e2e.yml");

let stack: Stack;

test.beforeAll(async () => {
  test.setTimeout(240_000); // a hook keeps the 30s default otherwise (see appmode-upload.spec.ts)
  stack = await startStack();
});

test.afterAll(async () => {
  await stack?.stop();
});

test("the real streamed app docks the relay plate; the floating bar stands down; Sign out ends the tunnel", async ({
  page,
}) => {
  await connect(page, stack);
  const plate = page.getByTestId("relay-plate");
  await expect(plate).toBeVisible({ timeout: 60_000 });
  await expect(plate).toHaveAccessibleName(/^Relay connection: linked — e2e-box/);
  // Past the shell's 2 s fallback window, its own bar is still down: the plate docked.
  await page.waitForTimeout(2500);
  await expect(page.locator("#session-box")).toBeHidden();

  await plate.click();
  await expect(page.getByTestId("relay-status")).toContainText("the relay is blind");
  await page.getByTestId("relay-sign-out").click();
  await expect(page.locator("body")).toHaveAttribute("data-state", "signed-out");
  await expect(page.getByTestId("relay-plate")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Connect" })).toBeVisible();
});
