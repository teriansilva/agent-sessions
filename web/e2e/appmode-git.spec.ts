import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { connect, PY_STACK_AVAILABLE, SESSION_UUID, startStack, type Stack } from "./appmode";
import { HOOK_TIMEOUT_MS } from "./harness";
import { clickHeadAction, FILES_ACTION } from "./headActions";

/** #806's acceptance evidence: a branch switch **through the tunnel** (#579).
 *
 *  The issue names this because CSRF/Origin rewriting across the relay is the boundary under
 *  test — a same-origin POST is not the same request once it has crossed a blind relay and been
 *  re-headered by the agent. Nothing here is mocked except the relay itself, which is blind by
 *  design (see `appmode.ts`): the connect page, the handshake, the mux, `tunnel.fetch`, the agent
 *  process, the app, and the git repository are all real.
 *
 *  Desktop only: this is a transport proof, not a layout one, and booting three processes twice
 *  buys nothing on a shared runner.
 */
test.describe.configure({ mode: "serial" });
// Booting three real processes and a browser through a real handshake is not a 30s test.
test.setTimeout(180_000);
test.skip(({ isMobile }) => !!isMobile, "transport proof — runs once, on desktop");
// `web-ci` has no Python by design, so this skips there and runs in `appmode-e2e.yml` (and
// locally). Skipping loudly beats a spec that quietly cannot exercise what it claims.
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv) — see .forgejo/workflows/appmode-e2e.yml");

let stack: Stack;

test.beforeAll(async () => {
  // `test.setTimeout()` at describe level governs TESTS, not hooks — a `beforeAll` keeps the
  // 30s default, and booting a relay, an app and an agent does not fit in it on a loaded shared
  // runner (it does locally on a warm cache, which is exactly why this passed here and failed in
  // CI). The hook has to raise its own.
  // Strictly greater than `STARTUP_BUDGET_MS`, so a slow boot rejects (and cleans up after
  // itself) instead of being killed mid-await with `stack` still unassigned.
  test.setTimeout(HOOK_TIMEOUT_MS);
  stack = await startStack();
});

test.afterAll(async () => {
  // Awaited: `stop()` waits for the process groups to exit before deleting the temp home.
  await stack?.stop();
});

const branchOf = (repo: string) =>
  execFileSync("git", ["-C", repo, "branch", "--show-current"], { encoding: "utf8" }).trim();

test("the panel switches a real branch through the relay, and the repo moves on disk", async ({
  page,
}) => {
  expect(branchOf(stack.repo)).toBe("master");

  await connect(page, stack);
  // The streamed SPA lists the seeded session; open it, then the file panel's GIT tab.
  // The sidebar renders each session as a link to `/s/<engine>/<uuid>`.
  await page.locator(`a[href*="${SESSION_UUID}"]`).first().click({ timeout: 30_000 });
  await clickHeadAction(page, FILES_ACTION, { timeout: 30_000 });
  await expect(page.locator("[data-file-panel]")).toBeVisible({ timeout: 20_000 });
  await page.getByRole("tab", { name: /Git/ }).click();
  await expect(page.locator("[data-git-tab]")).toBeVisible({ timeout: 20_000 });

  // Everything above proves the READ path crossed the tunnel. This is the write.
  await page.locator("[data-branch-trigger]").click();
  await expect(page.locator("[data-branch-menu]")).toBeVisible();
  await page.locator("[data-branch='other']").click();

  // The assertion that matters is on DISK, not on screen: a UI that says "switched" while the
  // repository did not move is exactly what a mocked test would happily report.
  await expect.poll(() => branchOf(stack.repo), { timeout: 30_000 }).toBe("other");
  // And the panel settles from the server's own post-write status, over the same tunnel.
  await expect(page.locator("[data-file-panel]")).toContainText("other", { timeout: 20_000 });
});

// The CSRF-through-the-relay negative deliberately lives in `tests/test_gitwrite.py`
// (`test_a_relayed_write_without_csrf_is_still_refused`), where a real `AppProxyTarget` serves a
// real app: proving it here would mean adding a `window.__tunnelFetch` seam to production code
// purely so a test could reach it, which is a worse trade than running the same assertion one
// layer down against the same components.
