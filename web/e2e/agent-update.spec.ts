/** One-click agent Update and Roll back (#1741), in a real browser on desktop and mobile.
 *
 *  Settings → Agents names `installed → available` from `/api/plugins` alone. Update is ONE
 *  confirmation, then the existing operations: install a new generation → verify it with the host's
 *  existing vendor login (no sign-in step) → enable it. A failed verification changes nothing.
 *  Roll back activates the generation that was active before, through the same `activate`.
 *
 *  Mocked API; the roster is the generated fixture plus one catalog-installed agent. */
import { expect, test, type Page } from "@playwright/test";

import type { PluginCatalog, PluginGeneration, PluginOperation, PluginReview } from "../src/types/plugins";
import { DASHBOARD_PATH } from "../src/lib/routes";
import { ROSTER } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

const AGENT = "sample-agent";
const OLD = "c0ef9b0a-322c-49c2-843e-d8c7564918c2";
const NEW = "5d2a8f61-7b3e-4c19-a0d4-6e8f2b1c9a77";
const OLD_RECIPE = "b".repeat(64);
const NEW_RECIPE = "e".repeat(64);

function review(version: string, recipe: string, id: string): PluginReview {
  return {
    id, plugin_id: AGENT, source: "signed", sequence: 9, digest: `${recipe.slice(0, 1)}`.repeat(63) + "1",
    recipe_digest: recipe, adopted_path: null, adopted_sha256: null, expires_at: 2_000_000_000,
    entry: {
      manifest: {
        identity: { id: AGENT, label: "Sample agent", version, publisher: "Example" },
        runtime: { kind: "pty" },
        install: { kind: "npm-prefix", authority: "registry.npmjs.org", package: "@example/sample", version },
        signin: { kind: "cli-subcommand", subcommand: "login" },
      },
      recipe: { artifacts: [{ url: `https://registry.npmjs.org/@example/sample/-/sample-${version}.tgz`, sha256: "c".repeat(64), destination: "." }] },
    },
  };
}
const CHECKS = ["binary", "version", "new"];
const passed = (r: PluginReview) => ({
  digest: r.digest, checked_at: 1, results: CHECKS.map((check) => ({ check, passed: true, detail: "Real check passed." })),
});
const LIVE = {
  id: AGENT, present: true, supports_new: true, supports_seed_start: false, seed_reason: null,
  bin: "/home/u/.local/share/battlelab/agents/sample-agent/bin/sample", label: "Sample agent", kind: "agent", runtime: "pty",
  display: { name: AGENT, badge: "sa", accent: "slate", id_prefix: null, order: 900 }, session_id: { mint: "pinned" },
};

/** `state`: "update" = 1.2.3 active, the catalog pins 1.3.0; "updated" = 1.3.0 active, 1.2.3 kept. */
async function mockApp(
  page: Page,
  state: "update" | "updated",
  opts: { verify?: boolean; onboarded?: boolean; disableDuringVerify?: boolean; verifyFailsOnce?: boolean; legacyTie?: boolean; laterSequenced?: boolean } = {},
) {
  const oldGen: PluginGeneration = { id: OLD, review: review("1.2.3", OLD_RECIPE, "r-old"), required_checks: CHECKS, verification: null };
  oldGen.verification = passed(oldGen.review);
  const newGen: PluginGeneration = { id: NEW, review: review("1.3.0", NEW_RECIPE, "r-new"), required_checks: CHECKS, verification: null };
  newGen.verification = passed(newGen.review);
  const activated = (id: string, gen: string, at: number): PluginOperation => ({
    id, plugin_id: AGENT, kind: "activate", state: "complete", created_at: at, updated_at: at,
    generation_id: gen, review: null, review_digest: null, error: null,
  });
  const data: PluginCatalog = {
    feed: { state: "ready", sequence: 41, error: null },
    catalog: [{ manifest: newGen.review.entry.manifest, digest: NEW_RECIPE, source: "signed", installable: true }],
    plugins: [state === "update"
      ? { id: AGENT, active: OLD, candidate: OLD, enabled: true, generations: [oldGen] }
      : { id: AGENT, active: NEW, candidate: NEW, enabled: true, generations: [oldGen, newGen] }],
    operations: state === "update" ? [activated("a-old", OLD, 1)] : [activated("a-old", OLD, 1), activated("a-new", NEW, 2)],
    roster_generation: 1, roster_revision: state === "update" ? "a-old" : "a-new",
  };
  if (opts.legacyTie) {
    // Pre-upgrade history: 1.1.0 then 1.2.3 activated in ONE second (no sequence), 1.3.0 after.
    const older: PluginGeneration = { id: "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", review: review("1.1.0", "a".repeat(64), "r-older"), required_checks: CHECKS, verification: null };
    older.verification = passed(older.review);
    data.plugins[0].generations = [older, oldGen, newGen];
    data.operations = [activated("a-older", older.id, 100), activated("a-old", OLD, 100), activated("a-new", NEW, 101)];
  }
  if (opts.laterSequenced) {
    // The same pre-upgrade tie (1.1.0 / 1.2.3 in one second, no sequence), then two updates after
    // the upgrade, each with its commit sequence: 1.3.0, then 1.4.0 (active).
    const older: PluginGeneration = { id: "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", review: review("1.1.0", "a".repeat(64), "r-older"), required_checks: CHECKS, verification: null };
    older.verification = passed(older.review);
    const newest: PluginGeneration = { id: "7e6d5c4b-3a29-4180-9f7e-6d5c4b3a2918", review: review("1.4.0", "d".repeat(64), "r-newest"), required_checks: CHECKS, verification: null };
    newest.verification = passed(newest.review);
    data.plugins[0] = { id: AGENT, active: newest.id, candidate: newest.id, enabled: true, generations: [older, oldGen, newGen, newest] };
    data.catalog[0] = { ...data.catalog[0], manifest: newest.review.entry.manifest, digest: "d".repeat(64) };
    data.operations = [activated("a-older", older.id, 100), activated("a-old", OLD, 100),
      { ...activated("a-new", NEW, 101), sequence: 1 }, { ...activated("a-newest", newest.id, 102), sequence: 2 }];
    data.roster_revision = "a-newest";
  }
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  const sockets: string[] = [];
  let clock = 10;

  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) => r.fulfill({ json: {
    csrf: "test-csrf", auth_mode: "none", username: null, terminal_backend: "ws",
    new_session_engines: ["claude"], onboarded: opts.onboarded ?? true,
  } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [...ROSTER.engines, LIVE], problems: [] } }));
  await page.route("**/api/sessions**", (r) => r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }));
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/missions**", (r) => r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }));
  await page.route("**/api/pulse/notifications**", (r) => r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }));
  await page.route("**/api/agents/usage", (r) => r.fulfill({ json: { budgets: { threshold_pct: 90, notify: true, engines: {} }, agents: [] } }));
  await page.route("**/api/plugins**", async (r) => {
    const req = r.request();
    const path = new URL(req.url()).pathname;
    if (req.method() === "GET") {
      if (path === "/api/plugins") return r.fulfill({ json: data });
      const found = data.operations.find((o) => path.endsWith(o.id));
      return r.fulfill({ status: found ? 200 : 409, json: found ?? { detail: "plugin operation not found" } });
    }
    const body = (req.postDataJSON() ?? {}) as Record<string, unknown>;
    writes.push({ path, body });
    if (path.endsWith("/review")) return r.fulfill({ json: { ...review("1.3.0", NEW_RECIPE, "r-fresh") } });
    if (path.endsWith("/reload")) return r.fulfill({ json: { generation: 2 } });
    const kind = path.split("/").at(-1) as PluginOperation["kind"];
    const id = body.request_id as string;
    const row = data.plugins[0];
    clock += 1;
    let result: PluginOperation;
    if (kind === "install") {
      const gen: PluginGeneration = { id, review: review("1.3.0", NEW_RECIPE, "r-fresh"), required_checks: CHECKS, verification: null };
      row.generations = [...(row.generations ?? []), gen];
      row.candidate = id;
      result = { id, plugin_id: AGENT, kind, state: "installed", created_at: clock, updated_at: clock, generation_id: id, review: gen.review, review_digest: gen.review.digest, error: null };
    } else {
      const gen = row.generations!.find((g) => g.id === body.generation_id)!;
      let state: PluginOperation["state"] = "complete";
      if (kind === "activate" && (("expected_revision" in body && body.expected_revision !== data.roster_revision) ||
        ("expected_active" in body && body.expected_active !== row.active))) {
        // The server's precondition (#1741): the state the activation was confirmed against moved.
        return r.fulfill({ status: 409, json: { detail: "the agent roster changed; refresh and confirm again" } });
      }
      if (kind === "verify" && opts.disableDuringVerify) {
        row.enabled = false; // the operator disabled the agent in another tab meanwhile
        data.roster_revision = "x-disabled";
      }
      if (kind === "verify") {
        const ok = opts.verifyFailsOnce ? data.operations.some((o) => o.kind === "verify") : opts.verify ?? true;
        gen.verification = { digest: gen.review.digest, checked_at: clock,
          results: CHECKS.map((check) => ({ check, passed: ok || check !== "new", detail: ok ? "Real check passed." : "The vendor rejected the request." })) };
        state = ok ? "verified" : "failed";
      }
      if (kind === "activate") { row.active = gen.id; row.candidate = gen.id; row.enabled = true; data.roster_revision = id; }
      result = { id, plugin_id: AGENT, kind, state, created_at: clock, updated_at: clock, generation_id: gen.id, review: null, review_digest: gen.review.digest,
        error: state === "failed" ? "required verification did not pass" : null };
    }
    data.operations.push(result);
    return r.fulfill({ json: result });
  });
  await page.routeWebSocket(/\/ws\/plugins\/signin\//, (ws) => { sockets.push(ws.url()); });
  return { data, writes, sockets };
}

const paths = (m: { writes: { path: string }[] }) => m.writes.map((w) => w.path);
async function agentRow(page: Page) {
  await page.goto("/settings/agents");
  await page.getByLabel("Search agents").fill("sample");
  const row = page.locator(`[data-engine="${AGENT}"]`);
  await expect(row).toBeVisible();
  return row;
}

test("Update is one confirmation, then install → verify → enable, with no sign-in step (#1741)", async ({ page }) => {
  const m = await mockApp(page, "update");
  const row = await agentRow(page);
  await expect(row.getByTestId("agent-versions")).toHaveText("1.2.3 → 1.3.0");
  await expect(row).toContainText("Update available");
  const update = row.getByRole("link", { name: "Update Sample agent to 1.3.0" });
  expect((await update.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  if (process.env.SHOTS_1741) await page.screenshot({ path: `${process.env.SHOTS_1741}/roster-update-${test.info().project.name}.png`, fullPage: true });
  await update.click();

  // The one confirmation: versions, source, account, running sessions — and what a failure means.
  await expect(page.getByRole("heading", { name: "Update Sample agent" })).toBeVisible();
  const summary = page.getByRole("region", { name: "Update summary" });
  await expect(summary.getByTestId("update-versions")).toHaveText("1.2.3 → 1.3.0");
  await expect(summary).toContainText("registry.npmjs.org · signed catalog cut #41");
  await expect(summary).toContainText("Keeps this host’s Sample agent sign-in");
  await expect(summary).toContainText("If verification fails, nothing changes: 1.2.3 stays active.");
  expect(m.writes).toEqual([]); // opening it changed nothing
  if (process.env.SHOTS_1741) await page.screenshot({ path: `${process.env.SHOTS_1741}/summary-${test.info().project.name}.png`, fullPage: true });
  await page.getByRole("button", { name: "Update to 1.3.0" }).click();

  await expect(page.getByText("Agent enabled.")).toBeVisible();
  // Exactly the existing operations, in order — and no sign-in: no signin POST, no terminal.
  expect(paths(m)).toEqual([
    "/api/plugins/review", "/api/plugins/install", "/api/plugins/verify", "/api/plugins/activate", "/api/plugins/reload",
  ]);
  expect(m.sockets).toEqual([]);
  expect(m.writes[1].body).toMatchObject({ confirm_local: false, confirm_adopted: false });
  expect(m.writes[2].body).toMatchObject({ plugin_id: AGENT, generation_id: m.writes[1].body.request_id });
  // The enable is bound to the state the update was confirmed against.
  expect(m.writes[3].body).toMatchObject({ generation_id: m.writes[1].body.request_id, expected_active: OLD, expected_revision: "a-old" });
  expect(m.data.plugins[0].active).toBe(m.writes[1].body.request_id);

  // Back on the roster: the new version is active, the old one kept for Roll back.
  await page.getByRole("link", { name: "Back to agents" }).click();
  await page.getByLabel("Search agents").fill("sample");
  await expect(row.getByTestId("agent-versions")).toHaveText("1.3.0 active · 1.2.3 kept");
  await expect(row.getByRole("button", { name: "Roll back to 1.2.3" })).toBeVisible();
  if (process.env.SHOTS_1741) await page.screenshot({ path: `${process.env.SHOTS_1741}/roster-rollback-${test.info().project.name}.png`, fullPage: true });
});

test("a failed verification changes nothing: the old version stays active (#1741)", async ({ page }) => {
  const m = await mockApp(page, "update", { verify: false });
  const row = await agentRow(page);
  await row.getByRole("link", { name: "Update Sample agent to 1.3.0" }).click();
  await page.getByRole("button", { name: "Update to 1.3.0" }).click();
  await expect(page.getByTestId("update-failed")).toContainText("Verification didn’t pass, so nothing changed: 1.2.3 stays active.");
  // The sign-in is offered now, through the flow's account step — never before.
  await expect(page.getByTestId("update-failed")).toContainText("Back to account setup");
  await expect(page.getByRole("button", { name: "Back to account setup" })).toBeVisible();
  expect(paths(m)).toEqual(["/api/plugins/review", "/api/plugins/install", "/api/plugins/verify"]);
  expect(m.data.plugins[0].active).toBe(OLD);
  await expect(page.getByRole("button", { name: "Enable agent" })).toHaveCount(0);
});

test("Roll back activates the previous generation through the activate operation (#1741)", async ({ page }) => {
  const m = await mockApp(page, "updated");
  const row = await agentRow(page);
  await expect(row.getByTestId("agent-versions")).toHaveText("1.3.0 active · 1.2.3 kept");
  const back = row.getByRole("button", { name: "Roll back to 1.2.3" });
  expect((await back.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await back.click();
  const ask = page.getByRole("dialog", { name: "Roll back Sample agent to 1.2.3?" });
  await expect(ask).toContainText("Running sessions keep the version they were started with");
  // Cancel changes nothing.
  await ask.getByRole("button", { name: "Cancel" }).click();
  expect(m.writes).toEqual([]);
  await back.click();
  await page.getByRole("dialog").getByRole("button", { name: "Roll back to 1.2.3" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(paths(m)).toEqual(["/api/plugins/activate", "/api/plugins/reload"]);
  expect(m.writes[0].body).toMatchObject({ plugin_id: AGENT, generation_id: OLD, expected_active: NEW, expected_revision: "a-new" });
  // 1.2.3 is active again, and the catalog's 1.3.0 is an update once more.
  await expect(row.getByTestId("agent-versions")).toHaveText("1.2.3 → 1.3.0");
});

test("the setup wizard's Agents step names the update and runs it inline (#1739/#1741)", async ({ page }) => {
  await mockApp(page, "update", { onboarded: false });
  await page.goto(DASHBOARD_PATH, { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: /set up battlelab/i });
  await expect(dialog.getByRole("heading", { name: "Welcome, Commander." })).toBeVisible();
  await dialog.getByRole("button", { name: /get started/i }).click();
  await dialog.getByRole("button", { name: /skip.*continue/i }).click();
  const row = dialog.locator(`[data-agent="${AGENT}"]`);
  await expect(row).toContainText("1.2.3 → 1.3.0");
  await row.getByRole("button", { name: `Update: ${AGENT}` }).click();
  const flow = row.getByRole("region");
  await expect(flow.getByTestId("update-versions")).toHaveText("1.2.3 → 1.3.0");
  await expect(flow.getByRole("button", { name: "Update to 1.3.0" })).toBeVisible();
});

test("an Update opened before the agent was disabled installs nothing (#1741)", async ({ page }) => {
  const m = await mockApp(page, "update");
  const row = await agentRow(page);
  await row.getByRole("link", { name: "Update Sample agent to 1.3.0" }).click();
  await expect(page.getByRole("button", { name: "Update to 1.3.0" })).toBeVisible();
  // Disabled in another tab while this confirmation was open.
  m.data.plugins[0].enabled = false;
  m.data.roster_revision = "x-disabled";
  await page.getByRole("button", { name: "Update to 1.3.0" }).click();
  await expect(page.getByRole("alert")).toContainText("This agent changed since the update was shown");
  expect(m.writes).toEqual([]);
  expect(m.data.plugins[0].enabled).toBe(false);
});

test("a disable during the update's verification refuses the enable; the agent stays disabled (#1741)", async ({ page }) => {
  const m = await mockApp(page, "update", { disableDuringVerify: true });
  const row = await agentRow(page);
  await row.getByRole("link", { name: "Update Sample agent to 1.3.0" }).click();
  await page.getByRole("button", { name: "Update to 1.3.0" }).click();
  await expect(page.getByRole("alert")).toContainText("the agent roster changed");
  expect(paths(m)).toEqual(["/api/plugins/review", "/api/plugins/install", "/api/plugins/verify", "/api/plugins/activate"]);
  expect(m.data.plugins[0]).toMatchObject({ enabled: false, active: OLD });
});

test("a stale or hand-made update link for a disabled agent offers nothing to confirm (#1741)", async ({ page }) => {
  const m = await mockApp(page, "update");
  m.data.plugins[0].enabled = false;
  await page.goto(`/settings/agents/setup/new?plugin=${AGENT}&update=1`);
  await expect(page.getByTestId("update-unavailable")).toContainText("not enabled on this host");
  await expect(page.getByRole("button", { name: /^Update to/ })).toHaveCount(0);
  expect(m.writes).toEqual([]);
});

test("Roll back stays offered beside a newer Update (#1741)", async ({ page }) => {
  const m = await mockApp(page, "updated");
  m.data.catalog[0] = { ...m.data.catalog[0], digest: "f".repeat(64),
    manifest: { ...m.data.catalog[0].manifest, identity: { ...m.data.catalog[0].manifest.identity, version: "1.4.0" },
      install: { ...m.data.catalog[0].manifest.install!, version: "1.4.0" } } };
  const row = await agentRow(page);
  await expect(row.getByTestId("agent-versions")).toHaveText("1.3.0 → 1.4.0");
  await expect(row.getByRole("link", { name: "Update Sample agent to 1.4.0" })).toBeVisible();
  const back = row.getByRole("button", { name: "Roll back to 1.2.3" });
  await expect(back).toBeVisible();
  expect((await back.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
});

const activations = (m: { writes: { path: string; body: Record<string, unknown> }[] }) =>
  m.writes.filter((w) => w.path === "/api/plugins/activate").map((w) => w.body);

test("after a refused enable, a manual Enable needs a fresh confirmation against the new state (#1741)", async ({ page }) => {
  const m = await mockApp(page, "update", { disableDuringVerify: true });
  const row = await agentRow(page);
  await row.getByRole("link", { name: "Update Sample agent to 1.3.0" }).click();
  await page.getByRole("button", { name: "Update to 1.3.0" }).click();
  await expect(page.getByRole("alert")).toContainText("the agent roster changed");
  expect(activations(m)).toHaveLength(1);
  // The fallback Enable never sends an unconditioned activation: it shows the new state first…
  await page.getByRole("button", { name: "Enable agent" }).click();
  const again = page.getByTestId("update-reconfirm");
  await expect(again).toContainText("was disabled or removed");
  await expect(again).toContainText("re-enables an agent that is disabled");
  expect(activations(m)).toHaveLength(1);
  // …"Leave it" sends nothing.
  await again.getByRole("button", { name: "Leave it as it is" }).click();
  expect(activations(m)).toHaveLength(1);
  expect(m.data.plugins[0].enabled).toBe(false);
  // Only an explicit confirmation against the CURRENT state enables, bound to that state.
  await page.getByRole("button", { name: "Enable agent" }).click();
  await page.getByTestId("update-reconfirm").getByRole("button", { name: "Enable 1.3.0 anyway" }).click();
  await expect(page.getByText("Agent enabled.")).toBeVisible();
  expect(activations(m)).toHaveLength(2);
  expect(activations(m)[1]).toMatchObject({ expected_active: OLD, expected_revision: "x-disabled" });
});

test("a failed verify, a retry, then Enable stays bound to the confirmed update (#1741)", async ({ page }) => {
  const m = await mockApp(page, "update", { verifyFailsOnce: true });
  const row = await agentRow(page);
  await row.getByRole("link", { name: "Update Sample agent to 1.3.0" }).click();
  await page.getByRole("button", { name: "Update to 1.3.0" }).click();
  await expect(page.getByTestId("update-failed")).toContainText("1.2.3 stays active");
  await page.getByRole("button", { name: "Run verification again" }).click();
  await page.getByRole("button", { name: "Enable agent" }).click();
  await expect(page.getByText("Agent enabled.")).toBeVisible();
  expect(activations(m)).toEqual([expect.objectContaining({ expected_active: OLD, expected_revision: "a-old" })]);
});

test("a reload after a refused enable never activates on the first click: it asks against the current state (#1741)", async ({ page }) => {
  const m = await mockApp(page, "update", { disableDuringVerify: true });
  const row = await agentRow(page);
  await row.getByRole("link", { name: "Update Sample agent to 1.3.0" }).click();
  await page.getByRole("button", { name: "Update to 1.3.0" }).click();
  await expect(page.getByRole("alert")).toContainText("the agent roster changed");
  expect(activations(m)).toHaveLength(1);
  // The page reloads: the update's confirmation (this mount's binding) is gone.
  await page.reload();
  await page.getByRole("button", { name: "Enable agent" }).click();
  const again = page.getByTestId("update-reconfirm");
  await expect(again).toContainText("Sample agent is disabled on this host (last on 1.2.3)");
  await expect(again).toContainText("re-enables an agent that is disabled");
  expect(activations(m)).toHaveLength(1); // the first click sent nothing
  expect(m.data.plugins[0].enabled).toBe(false);
  await again.getByRole("button", { name: "Enable 1.3.0 anyway" }).click();
  await expect(page.getByText("Agent enabled.")).toBeVisible();
  expect(activations(m)).toHaveLength(2);
  expect(activations(m)[1]).toMatchObject({ expected_active: OLD, expected_revision: "x-disabled" });
});

test("a reload of a verified update for an ENABLED agent also asks before replacing its version (#1741)", async ({ page }) => {
  const m = await mockApp(page, "update", { verifyFailsOnce: true });
  const row = await agentRow(page);
  await row.getByRole("link", { name: "Update Sample agent to 1.3.0" }).click();
  await page.getByRole("button", { name: "Update to 1.3.0" }).click();
  await page.getByRole("button", { name: "Run verification again" }).click();
  await expect(page.getByRole("button", { name: "Enable agent" })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Enable agent" }).click();
  await expect(page.getByTestId("update-reconfirm")).toContainText("Sample agent is on 1.2.3");
  await expect(page.getByTestId("update-reconfirm")).toContainText("replaces it; running sessions keep it");
  expect(activations(m)).toHaveLength(0);
  await page.getByTestId("update-reconfirm").getByRole("button", { name: "Enable 1.3.0 anyway" }).click();
  await expect(page.getByText("Agent enabled.")).toBeVisible();
  expect(activations(m)).toEqual([expect.objectContaining({ expected_active: OLD, expected_revision: "a-old" })]);
});

test("older history that can't order its versions offers no one-click Roll back, only a choice (#1741)", async ({ page }) => {
  const m = await mockApp(page, "updated", { legacyTie: true });
  const row = await agentRow(page);
  await expect(row.getByRole("button", { name: /^Roll back/ })).toHaveCount(0);
  const note = row.getByTestId("rollback-undetermined");
  await expect(note).toContainText("Previous version can’t be determined for older history");
  const choice = note.getByRole("link", { name: "Choose Sample agent 1.2.3" });
  await expect(note.getByRole("link", { name: "Choose Sample agent 1.1.0" })).toBeVisible();
  expect((await choice.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // The note wraps inside the row: the page never scrolls sideways (found at 412 px).
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  // A choice opens that version's own Enable step, which confirms against the current state first.
  await choice.click();
  await page.getByRole("button", { name: "Enable agent" }).click();
  await expect(page.getByTestId("update-reconfirm")).toContainText("Sample agent is on 1.3.0");
  expect(m.writes).toEqual([]);
});

test("an old unordered tie never blocks Roll back for later, sequenced updates (#1741)", async ({ page }) => {
  const m = await mockApp(page, "updated", { laterSequenced: true });
  const row = await agentRow(page);
  await expect(row.getByTestId("agent-versions")).toHaveText("1.4.0 active · 1.3.0 kept");
  await expect(row.getByTestId("rollback-undetermined")).toHaveCount(0);
  await row.getByRole("button", { name: "Roll back to 1.3.0" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Roll back to 1.3.0" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(m.writes[0]).toMatchObject({ path: "/api/plugins/activate", body: { generation_id: NEW, expected_revision: "a-newest" } });
});
