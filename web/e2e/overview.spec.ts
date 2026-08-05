import { expect, test } from "@playwright/test";

// Real-browser test of the overview map interactions (#149/#152). React Flow only makes a
// node hit-testable when it's selectable/draggable OR has a click handler; this proves the
// onNodeClick wiring works — clusters expand on click and chips open the session — which
// jsdom/component tests can't (no layout / pointer-events). Network is mocked so the map
// renders without a backend.

const now = Math.floor(Date.now() / 1000);
const sessions = [
  {
    id: "claude:aaa",
    engine: "claude",
    uuid: "aaa",
    short_uuid: "aaa",
    cwd: "/home/u/proj",
    project: { kind: "folder", id: "/home/u/proj", name: "proj" },
    last_mtime: now,
    first_user_message: "",
    title: "First session",
    sticky: false,
    archived: false,
  },
  {
    id: "opencode:bbb",
    engine: "opencode",
    uuid: "bbb",
    short_uuid: "bbb",
    cwd: "/home/u/proj",
    project: { kind: "folder", id: "/home/u/proj", name: "proj" },
    last_mtime: now - 1000,
    first_user_message: "",
    title: "Second session",
    sticky: false,
    archived: false,
  },
];

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions,
        next_offset: null,
        total: sessions.length,
        facets: {
          projects: [
            { kind: "folder", id: "/home/u/proj", name: "/home/u/proj" },
          ],
          engines: ["claude", "opencode"],
        },
      },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [{ cwd: "/home/u/proj", label: "proj" }] } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
});

test("desktop: a cluster expands on click, then a chip opens the session (#149)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "covered on desktop");
  await page.goto("/overview");

  // Scope to the overview canvas — the sidebar list also renders the session titles.
  const ov = page.locator(".tr-overview");
  // Unadopted sessions fold into the synthetic Default project in the Projects layout (#445).
  const header = ov.getByTitle(/expand Default/i);
  await expect(header).toBeVisible();
  // Collapsed by default → no chips in the canvas yet.
  await expect(ov.getByText("First session")).toHaveCount(0);

  // Click the cluster header → it expands and the chips render.
  await header.click();
  await expect(ov.getByText("First session")).toBeVisible();

  // Click a chip → it opens that session (URL = identity).
  await ov.getByText("First session").click();
  await expect(page).toHaveURL(/\/s\/claude\/aaa$/);
});

test("desktop: the layout selector regroups the map and persists device-locally (#424)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "selector covered on desktop");
  await page.goto("/overview");
  const ov = page.locator(".tr-overview");

  // Default = Projects: both sessions are unadopted → they fold into the Default project (#445).
  await expect(
    ov.getByRole("radio", { name: /group by projects/i }),
  ).toHaveAttribute("aria-checked", "true");
  await expect(ov.getByTitle(/expand Default/i)).toBeVisible();

  // Switch to Agents → one cluster per engine (claude + opencode), no folder cluster.
  await ov.getByRole("radio", { name: /group by agents/i }).click();
  await expect(
    ov.getByRole("radio", { name: /group by agents/i }),
  ).toHaveAttribute("aria-checked", "true");
  await expect(ov.getByTitle(/expand claude/i)).toBeVisible();
  await expect(ov.getByTitle(/expand opencode/i)).toBeVisible();
  await expect(ov.getByTitle(/expand \/home\/u\/proj/i)).toHaveCount(0);

  // The choice is device-local (localStorage) and survives a reload.
  expect(
    await page.evaluate(() => localStorage.getItem("tr-overview-groupby")),
  ).toBe("agent");
  await page.reload();
  await expect(
    ov.getByRole("radio", { name: /group by agents/i }),
  ).toHaveAttribute("aria-checked", "true");
  await expect(ov.getByTitle(/expand claude/i)).toBeVisible();
});
