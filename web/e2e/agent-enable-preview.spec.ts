import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";
import type { PluginActivationPreview, PluginCatalog, PluginGeneration, PluginReview } from "../src/types/plugins";
import { TOUCH_FLOOR } from "./touchTarget";

// #1852: a leftover copy (installed from an older catalog recipe) was enabled straight after a
// sign-in. It silently replaced the newer host binary and disabled the API client that needs a
// newer version. Setup's Enable step now shows what enabling changes (current → next, "older"),
// marks a stale copy and offers the catalog's version first, and cannot enable a version the
// server refuses for an API client's floor. Red on origin/main: none of it was on screen.
const PATH = "/settings/agents/setup/new";
const GEN = "c0ef9b0a-322c-49c2-843e-d8c7564918c2";
const REVIEW: PluginReview = {
  id: "e51cddbe-23e6-42bf-a994-51e08e9b087f", plugin_id: "sample-agent", source: "bundled", sequence: null,
  digest: "a".repeat(64), recipe_digest: "b".repeat(64), adopted_path: null, adopted_sha256: null,
  expires_at: 2_000_000_000,
  entry: { manifest: { identity: { id: "sample-agent", label: "Sample agent", version: "2.1.286", publisher: "Sample publisher" },
    runtime: { kind: "pty" }, install: { kind: "tarball", version: "2.1.286" }, signin: { kind: "cli-subcommand", subcommand: "login" } },
    recipe: { artifacts: [] } },
};
const CHECKS = ["binary", "version", "new", "resume", "transcript", "usage"];

const OTHER = "0d6c3f7e-5a2b-4c1d-9e8f-7a6b5c4d3e2f";

async function fixture(page: Page, preview: PluginActivationPreview, opts: { failPreview?: number; updateCandidate?: boolean } = {}) {
  const gen: PluginGeneration = { id: GEN, review: structuredClone(REVIEW), required_checks: CHECKS,
    verification: { digest: REVIEW.digest, checked_at: 1, results: CHECKS.map(check => ({ check, passed: true, detail: "Real check passed." })) } };
  const other: PluginGeneration = { ...structuredClone(gen), id: OTHER };
  const data: PluginCatalog = { feed: { state: "ready", sequence: 9, error: null },
    catalog: [{ manifest: { ...REVIEW.entry.manifest, identity: { ...REVIEW.entry.manifest.identity, version: "2.1.310" },
      install: { kind: "tarball", version: "2.1.310" } }, digest: "c".repeat(64), source: "bundled" }],
    plugins: [opts.updateCandidate
      ? { id: "sample-agent", active: OTHER, candidate: GEN, enabled: true, generations: [gen, other] }
      : { id: "sample-agent", active: null, candidate: GEN, enabled: null, generations: [gen] }],
    operations: [], roster_generation: 1, roster_revision: "r1" };
  const writes: string[] = [];
  let failPreview = opts.failPreview ?? 0;
  let release = () => {};
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/**", r => r.fulfill({ json: {} }));
  await page.route("**/api/config", r => r.fulfill({ json: {
    csrf: "test-csrf", new_session_engines: [], terminal_backend: "ws", auth_mode: "none",
    agent_defaults: { default_engine: "missing-saved-default", bypass: false },
  } }));
  await mockRoster(page);
  await page.route("**/api/sessions**", r => r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }));
  await page.route(/\/api\/projects(\?.*)?$/, r => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/folders(\?.*)?$/, r => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/missions**", r => r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }));
  await page.route("**/api/pulse/notifications**", r => r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }));
  await page.route("**/api/agents/usage", r => r.fulfill({ json: { budgets: { threshold_pct: 90, notify: true, engines: {} }, agents: [] } }));
  await page.route("**/api/plugins**", async r => {
    const path = new URL(r.request().url()).pathname;
    if (r.request().method() !== "GET") {
      writes.push(path);
      return r.fulfill({ status: 409, json: { detail: "not in this test" } });
    }
    if (path === "/api/plugins") return r.fulfill({ json: data });
    if (path === `/api/plugins/sample-agent/generations/${GEN}/activation`) {
      if (failPreview > 0) { failPreview -= 1; return r.fulfill({ status: 503, json: { detail: "unavailable" } }); }
      // After the roster moves, the new preview is held until the test releases it.
      if (data.roster_revision !== "r1") await held;
      return r.fulfill({ json: preview });
    }
    return r.fulfill({ status: 409, json: { detail: "plugin operation not found" } });
  });
  return { writes, release, move: () => { data.roster_revision = "r2"; } };
}

test("a stale, older copy below an API client's floor shows why and cannot be enabled", async ({ page }, testInfo) => {
  const m = await fixture(page, { current: "2.1.318", next: "2.1.286", older: true,
    refusals: [{ engine: "sample-api", label: "Sample — API", floor: "2.1.287" }] });
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  const stale = page.getByTestId("setup-stale");
  await expect(stale).toContainText("installed from an older catalog recipe");
  await expect(stale).toContainText("The catalog now offers 2.1.310");
  const instead = stale.getByRole("button", { name: "Review 2.1.310 instead" });
  await expect(instead).toBeVisible();
  await expect(page.getByTestId("enable-versions")).toHaveText(/2\.1\.318 → 2\.1\.286\s*older/);
  await expect(page.getByTestId("enable-refused")).toContainText("Sample — API needs Sample agent 2.1.287 or later");
  const enable = page.getByRole("button", { name: "Enable 2.1.286 anyway" });
  await expect(enable).toBeDisabled();
  // Reachable and unclipped on both projects; the page never scrolls sideways.
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  const box = await instead.boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(box!.x + box!.width).toBeLessThanOrEqual((page.viewportSize()?.width ?? 0) + 1);
  await page.screenshot({ path: testInfo.outputPath("enable-refused.png"), fullPage: true });
  // The catalog's version is one real click away; nothing was written.
  await instead.click();
  await expect(page).toHaveURL(new RegExp(`${PATH}\\?plugin=sample-agent$`));
  expect(m.writes).toEqual([]);
});

test("an older copy no client refuses names the downgrade before Enable", async ({ page }, testInfo) => {
  const m = await fixture(page, { current: "2.1.318", next: "2.1.286", older: true, refusals: [] });
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  await expect(page.getByTestId("enable-versions")).toHaveText(/2\.1\.318 → 2\.1\.286\s*older/);
  await expect(page.getByText("Keep 2.1.318 until they restart; new sessions start on the older 2.1.286.")).toBeVisible();
  await expect(page.getByTestId("enable-refused")).toHaveCount(0);
  // A stale copy no client refuses is still never the plain primary Enable: it is the explicit,
  // secondary "anyway" choice under the stale notice, whose primary offers the catalog's version.
  await expect(page.getByTestId("setup-stale").getByRole("button", { name: "Review 2.1.310 instead" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Enable agent" })).toHaveCount(0);
  const enable = page.getByRole("button", { name: "Enable 2.1.286 anyway" });
  await expect(enable).toBeEnabled();
  const box = await enable.boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  expect(box!.x + box!.width).toBeLessThanOrEqual((page.viewportSize()?.width ?? 0) + 1);
  await page.screenshot({ path: testInfo.outputPath("enable-older.png"), fullPage: true });
  expect(m.writes).toEqual([]);
  // Only that explicit click asks the server to enable it.
  await enable.click();
  await expect.poll(() => m.writes).toEqual(["/api/plugins/activate"]);
});

test("an unreadable preview keeps Enable off until the versions are read (Hermes 7701)", async ({ page }) => {
  const m = await fixture(page, { current: "2.1.318", next: "2.1.286", older: true, refusals: [] }, { failPreview: 1 });
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  const unavailable = page.getByTestId("enable-preview-unavailable");
  await expect(unavailable).toBeVisible();
  const enable = page.getByRole("button", { name: "Enable 2.1.286 anyway" });
  await expect(enable).toBeDisabled();
  await expect(unavailable).toContainText("can’t be enabled yet");
  await expect(page.getByTestId("enable-versions")).toHaveCount(0);
  await unavailable.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("enable-versions")).toHaveText(/2\.1\.318 → 2\.1\.286\s*older/);
  await expect(enable).toBeEnabled();
  expect(m.writes).toEqual([]);
});

test("a roster that moved before Enable waits for the new versions before its confirmation (Hermes 7701)", async ({ page }) => {
  const m = await fixture(page, { current: "2.1.300", next: "2.1.286", older: true, refusals: [] }, { updateCandidate: true });
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  await expect(page.getByTestId("enable-versions")).toBeVisible();
  m.move(); // another tab changed the agent: the next roster read has a new revision
  await page.getByRole("button", { name: "Enable 2.1.286 anyway" }).click();
  const confirm = page.getByTestId("update-reconfirm");
  await expect(confirm).toBeVisible();
  const again = confirm.getByRole("button", { name: "Enable 2.1.286 anyway" });
  // The new preview is still in flight: no versions for this roster yet, so nothing can be sent.
  await expect(page.getByTestId("enable-versions")).toHaveCount(0);
  await expect(again).toBeDisabled();
  expect(m.writes).toEqual([]);
  m.release();
  await expect(page.getByTestId("enable-versions")).toHaveText(/2\.1\.300 → 2\.1\.286\s*older/);
  await expect(again).toBeEnabled();
  await again.click();
  await expect.poll(() => m.writes).toEqual(["/api/plugins/activate"]);
});
