import { expect, test } from "@playwright/test";

// #445 — folders are a sub-property of projects. Real-browser proof (red before the fix) that:
//  1. the Projects layout draws projects→sessions only — unadopted sessions fold into a single
//     synthetic "Default" cluster, never per-folder nodes;
//  2. the Folders layout still draws folder nodes, each tagged with its owning-project badge
//     (the adopting entity, else Default);
//  3. the sidebar "Filter by project" dropdown lists project ENTITIES (incl. empty ones + the
//     Default catch-all), never folder paths.
// Network is mocked so the map + sidebar render without a backend.

const now = Math.floor(Date.now() / 1000);
const side = { kind: "project", id: "p-1", name: "Side", color: "#5fd7ff" };
const mk = (
  id: string,
  cwd: string,
  project: unknown,
  title: string,
  dt = 0,
) => ({
  id,
  engine: "claude",
  uuid: id.split(":")[1],
  short_uuid: id.split(":")[1],
  cwd,
  project,
  last_mtime: now - dt,
  first_user_message: "",
  title,
  sticky: false,
  archived: false,
});
const sessions = [
  mk("claude:a", "/home/u/app", side, "Adopted session"),
  mk(
    "claude:p",
    "/home/u/plain",
    { kind: "folder", id: "/home/u/plain", name: "plain" },
    "Plain",
    100,
  ),
  mk(
    "claude:s",
    "/home/u/scratch",
    { kind: "folder", id: "/home/u/scratch", name: "scratch" },
    "Scratch",
    200,
  ),
];
// Server facets (#445): project entities incl. a 0-count empty one, plus the Default aggregate.
const facets = {
  projects: [
    { kind: "project", id: "p-1", name: "Side", color: "#5fd7ff", count: 1 },
    { kind: "project", id: "p-2", name: "Empty", color: "", count: 0 },
    {
      kind: "project",
      id: "__default__",
      name: "Default",
      color: "",
      count: 2,
    },
  ],
  engines: ["claude"],
};

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
      json: { sessions, next_offset: null, total: sessions.length, facets },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
});

test("Projects layout: unadopted sessions fold into Default, no per-folder nodes (#445)", async ({
  page,
}) => {
  await page.goto("/overview");
  const ov = page.locator(".tr-overview");
  await expect(ov).toBeVisible();

  // The adopting entity + the Default catch-all are the only clusters.
  await expect(ov.getByTitle("Expand Side", { exact: true })).toBeVisible();
  await expect(ov.getByTitle("Expand Default", { exact: true })).toBeVisible();
  // No standalone folder nodes for the unadopted launch dirs.
  await expect(
    ov.getByTitle("Expand /home/u/plain", { exact: true }),
  ).toHaveCount(0);
  await expect(
    ov.getByTitle("Expand /home/u/scratch", { exact: true }),
  ).toHaveCount(0);

  // Default merges BOTH unadopted sessions.
  await ov.getByTitle("Expand Default", { exact: true }).click();
  await expect(ov.locator(".tr-ov-chip", { hasText: "Plain" })).toBeVisible();
  await expect(ov.locator(".tr-ov-chip", { hasText: "Scratch" })).toBeVisible();
});

test("Folders layout: each folder node carries its owning-project badge (#445)", async ({
  page,
}) => {
  await page.goto("/overview");
  const ov = page.locator(".tr-overview");
  await expect(ov).toBeVisible();
  await ov.getByRole("radio", { name: /group by folders/i }).click();

  // The adopted folder is badged with its entity; an unadopted folder is badged Default.
  const appNode = ov.locator(".tr-ov-group", {
    has: page.getByTitle("Expand /home/u/app", { exact: true }),
  });
  await expect(appNode.locator(".tr-ov-owner")).toHaveText("Side");
  const plainNode = ov.locator(".tr-ov-group", {
    has: page.getByTitle("Expand /home/u/plain", { exact: true }),
  });
  await expect(plainNode.locator(".tr-ov-owner")).toHaveText("Default");
});

test("sidebar dropdown lists project entities incl. Default, not folder paths (#445)", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name === "mobile",
    "sidebar dropdown covered on desktop",
  );
  await page.goto("/overview");
  const select = page.getByLabel("Filter by project");
  await expect(select).toBeVisible();
  // Project entities (incl. the empty one) + Default — never the launch-folder paths.
  await expect(select.getByRole("option", { name: "Side (1)" })).toHaveCount(1);
  await expect(select.getByRole("option", { name: "Empty (0)" })).toHaveCount(
    1,
  );
  await expect(select.getByRole("option", { name: "Default (2)" })).toHaveCount(
    1,
  );
  await expect(
    select.getByRole("option", { name: "/home/u/plain" }),
  ).toHaveCount(0);
  await expect(
    select.getByRole("option", { name: "/home/u/scratch" }),
  ).toHaveCount(0);
});
