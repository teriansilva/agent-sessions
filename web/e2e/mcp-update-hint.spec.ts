/** Library → MCP servers: "newer version available" (#2431), on a real browser at desktop and phone
 *  size, against `mcpFixture.ts`'s stand-in for the routes (the registry reads, the throttle and the
 *  identity fence are pinned in `tests/test_mcp_updates.py`). The pages, router and every request
 *  they make are real; what the requests SAID is asserted from the fake's call log.
 *
 *  Pinned: the index says "v3.4.0 available" beside the version, in the neutral info tone, only on
 *  the row with a newer release; a repository-link server's Overview and Settings → Source say
 *  "v3.4.0 published" with Update from source (never Update…); a package added by name offers
 *  Update…, which puts the release in the version field and installs nothing; Check for updates
 *  answers at once, shows Checking… while the page re-reads the server, settles, keeps a half-typed
 *  Settings form, and a second press inside 10 minutes says when it last looked; a failed read says
 *  "Couldn't check (…)" and keeps the last answer; an unknown hint is never "up to date"; Update
 *  from source's Review names the version it installs when the repository's head lags the
 *  registry, and says so when the link is up to date but a newer release exists. Every control
 *  is ≥ 44 px on the phone and nothing scrolls sideways. */
import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";

import { MCP_PATH, mcpServerPath, mcpServerUpdatePath } from "../src/lib/routes";
import type { McpUpdateHint } from "../src/types/mcp";
import { TOUCH_FLOOR } from "./touchTarget";
import {
  GO_NEW_SHA,
  GO_NEW_SUM,
  GO_SHA,
  NOW,
  emptyFake,
  goResolution,
  goServer,
  mockMcp,
  seededFake,
  type Fake,
} from "./mcpFixture";

test.setTimeout(60_000);

const isPhone = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;

// The issue's phone board: 390 × 844 (the mobile project's touch device, narrowed).
test.beforeEach(async ({ page }) => {
  if (isPhone(page)) await page.setViewportSize({ width: 390, height: 844 });
});
const H = 3600;

const hint = (over: Partial<McpUpdateHint> = {}): McpUpdateHint => ({
  state: "unknown",
  installed: "0.14.2",
  latest: null,
  checked_at: null,
  error: null,
  tried_at: null,
  checking: false,
  ...over,
});

/** The seeded `files` server (npm 0.14.2, installed, added by name). */
const files = (f: Fake) => f.servers.find((s) => s.name === "files")!;

async function noSideways(page: Page) {
  const { scroll, inner } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(inner);
}

async function tall(page: Page, testIds: string[]) {
  if (!isPhone(page)) return;
  for (const id of testIds) {
    const box = await page.getByTestId(id).first().boundingBox();
    expect(box, id).not.toBeNull();
    expect(box!.height, id).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
}

/** A capture for the PR, with `focus` scrolled into view (the app scrolls inside its own frame). */
async function shot(page: Page, info: TestInfo, name: string, focus?: Locator) {
  if (focus) await focus.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await page.screenshot({ path: info.outputPath(`${name}-${info.project.name}.png`) });
}

/** Nothing on these pages ever reviews or installs by itself. */
const installs = (f: Fake) => f.calls.filter((c) => /\/(review|install)$/.test(c.path));

test("the index: v3.4.0 available beside the version, neutral tone, only where a newer release is known", async ({
  page,
}, info) => {
  const f = seededFake();
  const go = goServer(f);
  f.updates[go.id] = hint({ state: "available", installed: "v3.3.0", latest: "v3.4.0", checked_at: NOW - 3 * H });
  f.updates[files(f).id] = hint({ state: "current", checked_at: NOW - 3 * H });
  await mockMcp(page, f);
  await page.goto(MCP_PATH);

  const rows = page.getByTestId("mcp-row");
  await expect(rows).toHaveCount(4);
  const chip = page.getByTestId("mcp-update-hint");
  await expect(chip).toHaveCount(1); // the current one and the remote ones say nothing
  const goRow = rows.filter({ has: page.getByText("Forgejo MCP") });
  await expect(goRow.getByTestId("mcp-update-hint")).toHaveText("v3.4.0 available"); // never "vv3.4.0"
  // Beside the version, in the source column; not a status.
  await expect(goRow.locator("[class*=src]").first()).toContainText("go · git.b4mad.industries/agentic-forges/forgejo-mcp/v3 v3.3.0");
  await expect(chip).not.toHaveAttribute("data-tone", /.*/);
  const [chipInk, bodyInk, statusInk] = await Promise.all([
    chip.evaluate((el) => getComputedStyle(el).color),
    goRow.locator("[class*=srcMain]").evaluate((el) => getComputedStyle(el).color),
    goRow.getByTestId("mcp-row-state").evaluate((el) => getComputedStyle(el).color),
  ]);
  expect(chipInk).toBe(bodyInk);
  expect(chipInk).not.toBe(statusInk);
  await noSideways(page);
  await shot(page, info, "1-index", goRow);
  expect(installs(f)).toEqual([]);
});

test("a repository-link server: Overview and Settings → Source say v3.4.0 published, with Update from source", async ({
  page,
}, info) => {
  const f = emptyFake();
  const s = goServer(f);
  f.updates[s.id] = hint({ state: "available", installed: "v3.3.0", latest: "v3.4.0", checked_at: NOW - 3 * H });
  await mockMcp(page, f);
  await page.goto(mcpServerPath(s.id));

  const block = page.getByTestId("mcp-overview-source");
  await expect(page.getByTestId("mcp-update-hint")).toHaveText("v3.4.0 available");
  await expect(block.getByTestId("mcp-update-answer")).toHaveText("v3.4.0 published · checked 3 h ago");
  await expect(block).toContainText("proxy.golang.org");
  await expect(block).not.toContainText(/up to date/i);
  const fromSource = block.getByTestId("mcp-update-from-source");
  await expect(fromSource).toHaveText("Update from source");
  await expect(fromSource).toHaveAttribute("href", mcpServerUpdatePath(s.id));
  await expect(block.getByTestId("mcp-update-package")).toHaveCount(0); // a link server never offers Update…
  await expect(block).toContainText("it may not have reached v3.4.0 yet");
  await tall(page, ["mcp-update-from-source", "mcp-update-check"]);
  await noSideways(page);
  await shot(page, info, "2-link-overview", block.getByTestId("mcp-update-line"));

  await page.getByRole("tab", { name: "Settings" }).click();
  const panel = page.getByTestId("mcp-source-panel");
  await expect(panel.getByTestId("mcp-update-answer")).toHaveText("v3.4.0 published · checked 3 h ago");
  await expect(panel.getByTestId("mcp-update-from-source")).toHaveCount(1); // once, not twice
  await expect(panel.getByTestId("mcp-update-check")).toHaveText("Check for updates");
  await tall(page, ["mcp-update-from-source", "mcp-update-check"]);
  await noSideways(page);
  await shot(page, info, "3-link-settings", panel.getByTestId("mcp-update-line"));
  await panel.getByTestId("mcp-update-from-source").click();
  await expect(page).toHaveURL(new RegExp(`${mcpServerUpdatePath(s.id)}$`));
});

test("a package added by name: Update… puts the published release in the version field and installs nothing", async ({
  page,
}, info) => {
  const f = seededFake();
  const s = files(f);
  f.updates[s.id] = hint({ state: "available", latest: "0.15.0", checked_at: NOW - 3 * H });
  await mockMcp(page, f);
  await page.goto(mcpServerPath(s.id));

  const block = page.getByTestId("mcp-overview-source");
  await expect(block.getByTestId("mcp-update-answer")).toHaveText("0.15.0 published · checked 3 h ago");
  await expect(block).toContainText("registry.npmjs.org");
  await expect(page.getByTestId("mcp-update-hint")).toHaveText("0.15.0 available");
  await expect(block.getByTestId("mcp-update-from-source")).toHaveCount(0);
  const update = block.getByTestId("mcp-update-package");
  await expect(update).toHaveText("Update…");
  await tall(page, ["mcp-update-package", "mcp-update-check"]);
  await noSideways(page);
  await shot(page, info, "4-package-overview", block.getByTestId("mcp-update-line"));

  await update.click();
  await expect(page).toHaveURL(/tab=settings/);
  const field = page.getByRole("textbox", { name: "files-mcp-server version" });
  await expect(field).toHaveValue("0.15.0");
  await expect(field).toBeFocused();
  expect(f.calls.filter((c) => c.method !== "GET")).toEqual([]); // nothing saved, reviewed or installed yet
  // Save is the operator's: then P1b's Review → Install.
  await page.getByTestId("mcp-settings-save").click();
  await expect(page.getByTestId("mcp-flash")).toHaveText("Saved the new version. Review and install it, then run the checks again.");
  const patch = f.calls.find((c) => c.method === "PATCH")!;
  expect((patch.body as { source: unknown }).source).toEqual({ kind: "npm", package: "files-mcp-server", version: "0.15.0" });
  await expect(page.getByTestId("mcp-install-panel")).toBeVisible();
  expect(installs(f)).toEqual([]);
});

test("Check for updates: answers at once, Checking… while it runs, settles, keeps the form; a second press is throttled", async ({
  page,
}, info) => {
  const f = seededFake();
  const s = files(f);
  f.updates[s.id] = hint();
  f.updatePolls = 3;
  f.updateResult[s.id] = { state: "available", latest: "0.15.0", checked_at: Math.floor(Date.now() / 1000) };
  await mockMcp(page, f);
  await page.goto(`${mcpServerPath(s.id)}?tab=settings`);

  const panel = page.getByTestId("mcp-source-panel");
  await expect(panel.getByTestId("mcp-update-answer")).toHaveText("Not checked yet");
  await expect(panel).not.toContainText(/up to date|no newer/i);
  // A half-typed edit elsewhere on Settings survives the background read.
  const args = page.getByRole("textbox", { name: "Arguments" });
  await args.fill("--readonly");

  const check = panel.getByTestId("mcp-update-check");
  await check.click();
  await expect(check).toHaveText("Checking…");
  await expect(check).toBeDisabled();
  await expect(panel.getByTestId("mcp-update-checking")).toHaveText("Checking registry.npmjs.org…");
  await shot(page, info, "5-checking", panel.getByTestId("mcp-update-line"));
  const posts = f.calls.filter((c) => c.path.endsWith("/updates/check"));
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toBeNull(); // no body: the server reads its own record

  await expect(panel.getByTestId("mcp-update-answer")).toHaveText("0.15.0 published · checked just now", { timeout: 15_000 });
  await expect(check).toHaveText("Check for updates");
  await expect(check).toBeEnabled();
  await expect(panel.getByTestId("mcp-update-package")).toBeVisible();
  await expect(args).toHaveValue("--readonly");

  // Inside the 10-minute window the server reads nothing again, and the page says when it looked.
  await check.click();
  await expect(panel.getByTestId("mcp-update-note")).toHaveText(
    /^Checked (just now|1 min ago)\. BattleLab reads a registry at most once every 10 minutes\.$/,
  );
  expect(f.calls.filter((c) => c.path.endsWith("/updates/check"))).toHaveLength(2);
  expect(Object.keys(f.updateRunning)).toEqual([]);
  await tall(page, ["mcp-update-check"]);
  await noSideways(page);
  expect(installs(f)).toEqual([]);
  expect(f.calls.filter((c) => c.method === "PATCH")).toEqual([]);
});

test("couldn't check: the registry's failure in words, the last answer kept", async ({ page }, info) => {
  const f = seededFake();
  const s = files(f);
  f.updates[s.id] = hint({ state: "current", checked_at: NOW - 26 * H, error: "the npm answered 503", tried_at: NOW - 300 });
  // The page's "now" is the fixture's: minutes are asserted, and NOW was taken when the fixture
  // loaded, which can be minutes before this test runs in a long single-worker run.
  await page.clock.setFixedTime(NOW * 1000);
  await mockMcp(page, f);
  await page.goto(`${mcpServerPath(s.id)}?tab=settings`);
  const panel = page.getByTestId("mcp-source-panel");
  await expect(panel.getByTestId("mcp-update-failed")).toHaveText("Couldn’t check (the npm answered 503) · 5 min ago");
  await expect(panel.getByRole("term").filter({ hasText: "Last answer" })).toBeVisible();
  await expect(panel.getByTestId("mcp-update-answer")).toHaveText("No newer release · checked 1 d ago");
  await expect(panel.getByTestId("mcp-update-package")).toHaveCount(0);
  await tall(page, ["mcp-update-check"]);
  await noSideways(page);
  await shot(page, info, "6-couldnt-check", panel.getByTestId("mcp-update-line"));
});

test("unknown is never up to date: not checked yet, and an answer older than 3 days", async ({ page }, info) => {
  const f = seededFake();
  const s = files(f);
  f.updates[s.id] = hint({ checked_at: NOW - 4 * 24 * H });
  await mockMcp(page, f);
  await page.goto(mcpServerPath(s.id));
  const block = page.getByTestId("mcp-overview-source");
  await expect(block.getByTestId("mcp-update-answer")).toHaveText("Last checked over 3 days ago");
  await expect(block).not.toContainText(/up to date|no newer/i);
  await expect(page.getByTestId("mcp-update-hint")).toHaveCount(0);
  await shot(page, info, "7-unknown", block.getByTestId("mcp-update-line"));

  // A remote server has no version: nothing at all.
  const remote = f.servers.find((x) => x.name === "tracker")!;
  await page.goto(mcpServerPath(remote.id));
  await expect(page.getByTestId("mcp-server-page")).toBeVisible();
  await expect(page.getByTestId("mcp-update-block")).toHaveCount(0);
  await expect(page.getByTestId("mcp-update-line")).toHaveCount(0);
});

test("Update from source when the repository's head lags the registry: Review names what it installs", async ({
  page,
}, info) => {
  const f = emptyFake();
  const s = goServer(f);
  f.updates[s.id] = hint({ state: "available", installed: "v3.3.0", latest: "v3.4.0", checked_at: NOW - 3 * H });
  f.links[s.origin!.url] = {
    resolution: goResolution({ version: "v3.3.1", commit: GO_NEW_SHA, sum: GO_NEW_SUM, reviewId: "c1d2e3f4a5b6c7d8" }),
  };
  await mockMcp(page, f);
  await page.goto(mcpServerUpdatePath(s.id));

  const review = page.getByTestId("mcp-update-review");
  await expect(review).toBeVisible();
  await expect(page.getByTestId("mcp-update-lag")).toHaveText(
    "The repository’s head declares v3.3.1; the registry’s newest release is v3.4.0. Install v3.3.1, or wait until the repository catches up.",
  );
  await expect(review.getByTestId("mcp-change").nth(1)).toHaveText("v3.3.0 → v3.3.1"); // the ACTUAL target
  await expect(review).toContainText("Install builds exactly v3.3.1 at this hash");
  await noSideways(page);
  await shot(page, info, "8-lagging-review", page.getByTestId("mcp-update-lag"));
  expect(f.calls.filter((c) => c.method === "PATCH")).toEqual([]);
});

test("Update from source already up to date while a newer release exists: the repository hasn't caught up", async ({
  page,
}, info) => {
  const f = emptyFake();
  const s = goServer(f);
  f.updates[s.id] = hint({ state: "available", installed: "v3.3.0", latest: "v3.4.0", checked_at: NOW - 3 * H });
  f.links[s.origin!.url] = { resolution: goResolution() };
  await mockMcp(page, f);
  await page.goto(mcpServerUpdatePath(s.id));
  await expect(page.getByTestId("mcp-update-current")).toHaveText(`Already up to date at v3.3.0 (${GO_SHA.slice(0, 7)})`);
  await expect(page.getByTestId("mcp-update-lag")).toHaveText(
    "The repository’s head declares v3.3.0; the registry’s newest release is v3.4.0. There is nothing newer to install from the repository until it catches up.",
  );
  await expect(page.getByRole("button", { name: "Install" })).toHaveCount(0);
  await noSideways(page);
  await shot(page, info, "9-uptodate-lagging", page.getByTestId("mcp-update-lag"));
});
