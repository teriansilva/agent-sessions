import { expect, test } from "@playwright/test";
import { ROSTER } from "./roster";
import { ID, mockStructuredGuidance } from "./structuredGuidanceFixture";
import { TOUCH_FLOOR } from "./touchTarget";

// An API session's turn failed because the agent's login expired (`remedy: "signin"`, from Claude's
// `error: authentication_failed`): next to the failed chip, a primary Sign in to the console agent
// whose login the session uses, opening Settings → Agents setup at its waiting candidate
// generation. Red on origin/main: the failed chip was all there was.
const ENGINE = "claude-api";
const SOURCE = ROSTER.engines.find((e) => e.id === ENGINE)!.api as { source: string };
const LABEL = ROSTER.engines.find((e) => e.id === SOURCE.source)!.label as string;
const FAILED = "33333333-2222-4333-8444-555555555555";
const PLAIN = "44444444-2222-4333-8444-555555555555";
const EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed";

async function setup(page: Parameters<typeof mockStructuredGuidance>[0], candidate: string | null = "gen-new") {
  const { state } = await mockStructuredGuidance(page, ENGINE, "interrupt");
  const base = { text_truncated: false, reply_truncated: false, tools_truncated: false, tools: [], reason: null };
  Object.assign(state, { state: "idle", active_turn: null });
  state.turns.splice(0, state.turns.length,
    { ...base, turn_id: PLAIN, operation_id: PLAIN, state: "failed", text: "Run the suite.",
      reply: "The tool crashed.", reason: "the agent ended this turn without its result" },
    { ...base, turn_id: FAILED, operation_id: FAILED, state: "failed", text: "hi", reply: EXPIRED,
      remedy: "signin" },
  );
  const plugins: string[] = [];
  await page.route("**/api/plugins", (r) => {
    plugins.push(r.request().url());
    return r.fulfill({ json: {
      plugins: [{ id: SOURCE.source, enabled: true, active: "gen-active", candidate: candidate ?? "gen-active",
        generations: candidate ? [{ id: "gen-active" }, { id: candidate }] : [{ id: "gen-active" }] }],
      catalog: [], operations: [], feed: { state: "ready", sequence: 1, error: null },
    } });
  });
  return plugins;
}

test("a sign-in failure offers Sign in to the console agent, and it opens that agent's setup", async ({ page }, testInfo) => {
  const plugins = await setup(page);
  await page.goto(`/s/${ENGINE}/${ID}`);
  const turns = page.getByTestId("structured-turn");
  await expect(turns).toHaveCount(2);

  // A failure without the remedy is unchanged: the chip, no Sign in.
  await expect(turns.nth(0).getByTestId("structured-failed")).toBeVisible();
  await expect(turns.nth(0).getByTestId("structured-signin")).toHaveCount(0);

  const failed = turns.nth(1);
  const chip = failed.getByTestId("structured-failed");
  await expect(chip).toHaveText(/^failed/);
  const link = failed.getByRole("link", { name: `Sign in to ${LABEL}` });
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute("href", "/settings/agents/setup/new?plugin=" + SOURCE.source + "&generation=gen-new&step=signin");
  expect(plugins.length).toBeGreaterThan(0);

  // Right after the chip, inside the pane, not clipped.
  const [chipBox, linkBox] = await Promise.all([chip.boundingBox(), link.boundingBox()]);
  expect(linkBox!.y).toBeGreaterThanOrEqual(chipBox!.y + chipBox!.height - 1);
  expect(linkBox!.x + linkBox!.width).toBeLessThanOrEqual((page.viewportSize()?.width ?? 0) + 1);
  expect(linkBox!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await page.screenshot({ path: testInfo.outputPath("signin-failed-turn.png"), fullPage: true });

  // A real click (no overlay in the way) lands on that agent's setup.
  await link.click();
  await expect(page).toHaveURL(
    new RegExp(`/settings/agents/setup/new\\?plugin=${SOURCE.source}&generation=gen-new&step=signin$`),
  );
});

test("with only the active copy installed, Sign in says it opens setup and disables nothing", async ({ page }) => {
  await setup(page, null);
  const writes: string[] = [];
  page.on("request", (r) => { if (r.method() !== "GET" && r.url().includes("/api/")) writes.push(r.url()); });
  await page.goto(`/s/${ENGINE}/${ID}`);
  const failed = page.getByTestId("structured-turn").nth(1);
  const link = failed.getByRole("link", { name: `Sign in to ${LABEL}` });
  await expect(link).toHaveAttribute("href", "/settings/agents/setup/new?plugin=" + SOURCE.source);
  const note = failed.getByTestId("structured-signin-via-setup");
  await expect(note).toContainText("review a new copy, then sign in there");
  await expect(note).toContainText("keep running");
  await link.click();
  await expect(page).toHaveURL(new RegExp(`/settings/agents/setup/new\\?plugin=${SOURCE.source}$`));
  // Opening setup is a navigation, never a disable / activate / any other write.
  expect(writes.filter((u) => /\/api\/plugins\//.test(u))).toEqual([]);
});

// #1852: the waiting candidate was a leftover installed from an OLDER catalog recipe. Signing in
// there and enabling it replaced a newer host binary and disabled the API client. A stale copy is
// never the target: Sign in opens a fresh setup from the current catalog instead.
test("a stale leftover copy is never where Sign in goes", async ({ page }) => {
  await setup(page);
  await page.route("**/api/plugins", (r) => r.fulfill({ json: {
    plugins: [{ id: SOURCE.source, enabled: null, active: null, candidate: "gen-old",
      generations: [{ id: "gen-old", review: { plugin_id: SOURCE.source, source: "bundled", recipe_digest: "a".repeat(64) } }] }],
    catalog: [{ manifest: { identity: { id: SOURCE.source, label: LABEL, publisher: "p", version: "2.1.310" } }, digest: "b".repeat(64) }],
    operations: [], feed: { state: "ready", sequence: 1, error: null },
  } }));
  await page.goto(`/s/${ENGINE}/${ID}`);
  const failed = page.getByTestId("structured-turn").nth(1);
  const link = failed.getByRole("link", { name: `Sign in to ${LABEL}` });
  await expect(failed.getByTestId("structured-signin-via-setup")).toContainText("you don’t need to enable the new copy");
  await expect(link).toHaveAttribute("href", "/settings/agents/setup/new?plugin=" + SOURCE.source);
});

// The waiting copy had already passed verification, so setup opened it at Enable ("Ready to
// enable", Verify / Enable): Sign in led to verification, never to a sign-in. Red on origin/main.
test("Sign in opens the sign-in step even when the waiting copy is already verified", async ({ page }) => {
  await setup(page);
  const checks = ["binary", "version"];
  const review = {
    id: "r-1", plugin_id: SOURCE.source, source: "bundled", sequence: null, digest: "d".repeat(64),
    recipe_digest: "e".repeat(64), adopted_path: null, adopted_sha256: null, expires_at: 2_000_000_000,
    entry: { manifest: { identity: { id: SOURCE.source, label: LABEL, publisher: "battlelab", version: "2" },
      install: { kind: "npm-prefix" }, signin: { kind: "auth-login" } }, recipe: { artifacts: [] } },
  };
  const verified = { id: "gen-new", review, required_checks: checks,
    verification: { digest: review.digest, checked_at: 1, results: checks.map((check) => ({ check, passed: true, detail: "ok" })) } };
  await page.route("**/api/plugins", (r) => r.fulfill({ json: {
    plugins: [{ id: SOURCE.source, enabled: true, active: "gen-active", candidate: "gen-new",
      generations: [{ ...verified, id: "gen-active" }, verified] }],
    catalog: [], operations: [], feed: { state: "ready", sequence: 1, error: null }, roster_revision: null,
  } }));
  const writes: string[] = [];
  page.on("request", (r) => { if (r.method() !== "GET" && r.url().includes("/api/plugins")) writes.push(r.url()); });
  await page.goto(`/s/${ENGINE}/${ID}`);
  await page.getByTestId("structured-turn").nth(1).getByRole("link", { name: `Sign in to ${LABEL}` }).click();
  await expect(page.getByRole("heading", { name: "Connect your account" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start sign-in", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Ready to enable" })).toHaveCount(0);
  // Opening it starts nothing: the sign-in begins on the operator's click.
  expect(writes).toEqual([]);
});
