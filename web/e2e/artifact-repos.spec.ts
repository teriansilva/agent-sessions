/** Shared artifact repositories (#2347 P1) in a real browser, desktop and mobile, over a stateful
 *  mock of `/api/repos` and `/api/artifacts`: the REPOSITORIES tab beside My artifacts, its empty
 *  state, New repository, a repository's page with its storage bar, "Add an artifact…", Open (the
 *  item's sandboxed link, in a new tab), Remove behind a confirm, and the 44 px floor with no
 *  sideways scroll at ≤800px. */
import { expect, test, type Page, type Route } from "@playwright/test";

import { ARTIFACTS_PATH, ARTIFACT_REPOS_PATH } from "../src/lib/routes";
import { CONFIG, commonMocks } from "./mission-directions";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";
import { animationsSettled } from "./nightjarShell";

const NOW = Math.floor(Date.now() / 1000);
const REPO_ID = "4821-3307-9154-6602";
const ART_ID = "5449-5592-2107-4372";
const QUOTA = 2 * 2 ** 30;

type Json = Record<string, unknown>;
interface State {
  repos: Json[];
  items: Json[];
  calls: { method: string; path: string; body: unknown }[];
}

const ARTIFACT = {
  id: ART_ID,
  title: "Brand guidelines 2027",
  why: "The brand book",
  kind: "file",
  revision: 2,
  created_at: NOW - 7200,
  updated_at: NOW - 720,
  origin: {
    engine: "claude",
    session_key: "claude:7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001",
    cwd: "/home/u/brand",
    project_id: null,
    mission_id: null,
  },
  visibility: "private",
  url: `/artifacts/${ART_ID}/`,
  public_url: null,
  public_since: null,
  files: 1,
  bytes: 8_283_750,
  entry: "guidelines.pdf",
};

async function mock(page: Page): Promise<State> {
  await commonMocks(page);
  await mockRoster(page);
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/missions($|\?)/, (r) =>
    r.fulfill({
      json: { missions: [], total: 0, limit: 200, offset: 0, facets: { projects: [], states: [] } },
    }),
  );
  const st: State = { repos: [], items: [], calls: [] };
  const view = (r: Json) => {
    const items = st.items.filter((i) => i.repository_id === r.id);
    return {
      ...r,
      items: items.length,
      bytes: items.reduce((n, i) => n + (i.bytes as number), 0),
    };
  };
  await page.route("**/api/artifacts**", (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/artifacts")
      return route.fulfill({ json: { items: [ARTIFACT], total: 1, next_cursor: null } });
    if (path === "/api/artifacts/settings")
      return route.fulfill({
        json: {
          enabled: true,
          artifacts: 1,
          public: 0,
          bytes: 8_283_750,
          caps: { revision_bytes: 209_715_200, revision_files: 2000, revisions: 20, total_bytes: 5 * QUOTA },
        },
      });
    return route.fulfill({ status: 404, json: { detail: "no such artifact" } });
  });
  await page.route("**/api/repos**", async (route: Route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();
    const body = req.postData() ? JSON.parse(req.postData()!) : null;
    st.calls.push({ method, path, body });
    if (method !== "GET" && req.headers()["x-csrf-token"] !== CONFIG.csrf)
      return route.fulfill({ status: 403, json: { detail: "csrf" } });
    if (path === "/api/repos") {
      if (method === "POST") {
        const repo = {
          id: REPO_ID,
          name: body.name,
          description: body.description,
          quota_bytes: QUOTA,
          created_at: NOW,
          updated_at: NOW,
        };
        st.repos.push(repo);
        return route.fulfill({ status: 201, json: view(repo) });
      }
      return route.fulfill({ json: { repositories: st.repos.map(view), quota_default: QUOTA } });
    }
    const m = path.match(/^\/api\/repos\/([0-9-]+)(\/items)?(?:\/([0-9-]+))?$/);
    const repo = st.repos.find((r) => r.id === m?.[1]);
    if (!repo) return route.fulfill({ status: 404, json: { detail: "There is no such repository." } });
    if (!m![2]) return route.fulfill({ json: view(repo) });
    if (method === "POST") {
      const item = {
        id: "1846-2207-3391-5520",
        repository_id: repo.id,
        title: ARTIFACT.title,
        why: ARTIFACT.why,
        kind: ARTIFACT.kind,
        revision: 1,
        added_by: { kind: "operator" },
        source_artifact: ART_ID,
        created_at: NOW,
        updated_at: NOW,
        files: 1,
        bytes: ARTIFACT.bytes,
        entry: ARTIFACT.entry,
        url: "/artifacts/i/1846-2207-3391-5520/",
      };
      st.items.push(item);
      return route.fulfill({ status: 201, json: item });
    }
    if (method === "DELETE") {
      st.items = st.items.filter((i) => i.id !== m![3]);
      return route.fulfill({ json: { removed: m![3] } });
    }
    const items = st.items.filter((i) => i.repository_id === repo.id);
    return route.fulfill({ json: { items, total: items.length, next_cursor: null } });
  });
  // The item's hosted bytes (in the new tab Open makes, so on the context): the real server
  // answers them sandboxed behind a view token.
  await page.context().route(/\/artifacts\/i\/[0-9-]+(@\d+)?\/.*$/, (r) =>
    r.fulfill({
      contentType: "text/html",
      headers: { "Content-Security-Policy": "sandbox allow-scripts" },
      body: "<!doctype html><title>Item</title><h1>Repository item</h1>",
    }),
  );
  return st;
}

test("the Repositories tab: empty, New repository, add an artifact, open it, remove it", async ({
  page,
  context,
}) => {
  const st = await mock(page);
  await page.goto(ARTIFACTS_PATH);
  await page.getByRole("tab", { name: "Repositories" }).click();
  await expect(page).toHaveURL(new RegExp(`${ARTIFACT_REPOS_PATH}$`));
  await expect(page.getByRole("tab", { name: "Repositories" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByTestId("repos-none")).toContainText("No repositories here yet");

  // New repository → its page, empty.
  await page.getByTestId("repo-new").click();
  const dialog = page.getByTestId("repo-new-dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("textbox", { name: /Name/ }).fill("design-assets");
  await dialog.getByRole("textbox", { name: /Description/ }).fill("Files several BattleLabs use");
  await dialog.getByRole("button", { name: "Create repository" }).click();
  await expect(page.getByRole("heading", { name: "design-assets" })).toBeVisible();
  await expect(page.getByTestId("repo-empty")).toBeVisible();
  await expect(page.getByTestId("repo-storage")).toContainText("0 B of 2 GB");
  expect(st.calls.find((c) => c.method === "POST" && c.path === "/api/repos")?.body).toEqual({
    name: "design-assets",
    description: "Files several BattleLabs use",
  });

  // Add an artifact… → the item, its kind tag and size; the storage bar moves.
  await page.getByTestId("repo-add").click();
  const add = page.getByTestId("repo-add-dialog");
  await add.getByRole("radio", { name: /Brand guidelines 2027/ }).check();
  await add.getByRole("button", { name: "Add to repository" }).click();
  await expect(add).toHaveCount(0);
  const row = page.locator("[data-item='1846-2207-3391-5520']");
  await expect(row).toBeVisible();
  await expect(row.locator("[data-kind]:visible").first()).toHaveText("PDF");
  await expect(row).toContainText("this BattleLab");
  await expect(page.getByTestId("repo-storage")).toContainText("7.9 MB of 2 GB");
  expect(st.calls.find((c) => c.method === "POST" && c.path.endsWith("/items"))?.body).toEqual({
    artifact_id: ART_ID,
  });

  // Open: the item's own sandboxed link, in a new tab.
  const [tab] = await Promise.all([
    context.waitForEvent("page"),
    row.getByRole("link", { name: /^Open / }).click(),
  ]);
  await tab.waitForLoadState();
  expect(new URL(tab.url()).pathname).toBe("/artifacts/i/1846-2207-3391-5520/");
  await expect(tab.getByRole("heading", { name: "Repository item" })).toBeVisible();
  await tab.close();

  // Remove behind a confirm (focus starts on Cancel).
  await row.getByRole("button", { name: /^Remove / }).click();
  const confirm = page.getByTestId("repo-confirm");
  await expect(confirm).toContainText("Remove “Brand guidelines 2027” from design-assets?");
  await expect(confirm.getByRole("button", { name: "Cancel" })).toBeFocused();
  await confirm.getByRole("button", { name: "Remove" }).click();
  await expect(row).toHaveCount(0);
  await expect(page.getByTestId("repo-empty")).toBeVisible();
  await expect(page.getByRole("status")).toContainText("is removed from design-assets");

  // Back to the list: the repository with its facts.
  await page.getByRole("link", { name: "Repositories" }).click();
  await expect(page.locator(`[data-repo='${REPO_ID}']`)).toContainText("0 items · 0 B of 2 GB");
});

test("every repositories control holds the 44 px floor at ≤800px, with no sideways scroll", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const st = await mock(page);
  st.repos.push({
    id: REPO_ID,
    name: "a-rather-long-repository-name-for-release-builds-and-reports",
    description: "Files several BattleLabs use, and a description long enough to wrap twice",
    quota_bytes: QUOTA,
    created_at: NOW,
    updated_at: NOW,
  });
  st.items.push({
    id: "1846-2207-3391-5520",
    repository_id: REPO_ID,
    title: "Onboarding screens v4 with a long title that must ellipsize on a phone",
    why: "Screens",
    kind: "site",
    revision: 4,
    added_by: { kind: "session", session_key: "claude:7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001" },
    source_artifact: null,
    created_at: NOW,
    updated_at: NOW,
    files: 38,
    bytes: 19_293_798,
    entry: "index.html",
    url: "/artifacts/i/1846-2207-3391-5520/",
  });
  const short: string[] = [];
  const measure = async (scope: string) => {
    await animationsSettled(page);
    const boxes = await page.locator(scope).evaluateAll((els) =>
      els
        .filter((el) => (el as HTMLElement).offsetParent !== null)
        .map((el) => ({
          name: el.getAttribute("aria-label") || el.textContent?.trim().slice(0, 30) || el.tagName,
          h: el.getBoundingClientRect().height,
        })),
    );
    for (const b of boxes) if (b.h < TOUCH_FLOOR) short.push(`${scope}: ${b.name} ${b.h}px`);
    return boxes.length;
  };
  const noSideways = async (testId: string) =>
    expect(
      await page.getByTestId(testId).evaluate((el) => el.scrollWidth - el.clientWidth),
    ).toBeLessThanOrEqual(0);

  await page.goto(ARTIFACT_REPOS_PATH);
  await expect(page.locator(`[data-repo='${REPO_ID}']`)).toBeVisible();
  expect(
    await measure("[data-testid='artifact-repos-page'] :is(button, a[href], [role='tab'])"),
  ).toBeGreaterThanOrEqual(3);
  await noSideways("artifact-repos-page");
  await page.getByTestId("repo-new").click();
  await expect(page.getByTestId("repo-new-dialog")).toBeVisible();
  await measure("[data-testid='repo-new-dialog'] :is(button, input)");
  await page.keyboard.press("Escape");

  await page.locator(`[data-repo='${REPO_ID}'] a`).click();
  await expect(page.locator("[data-item]")).toHaveCount(1);
  expect(
    await measure("[data-testid='artifact-repo-page'] :is(button, a[href])"),
  ).toBeGreaterThanOrEqual(4);
  await noSideways("artifact-repo-page");
  await page.getByTestId("repo-add").click();
  await expect(page.getByTestId("repo-add-dialog")).toBeVisible();
  await page.getByTestId("repo-add-dialog").getByRole("radio").first().check();
  await measure("[data-testid='repo-add-dialog'] :is(button, input[type='search'], select, label[data-pick])");
  expect(short).toEqual([]);
});
