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
  await expect(link).toHaveAttribute("href", "/settings/agents/setup/new?plugin=" + SOURCE.source + "&generation=gen-new");
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
    new RegExp(`/settings/agents/setup/new\\?plugin=${SOURCE.source}&generation=gen-new$`),
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
