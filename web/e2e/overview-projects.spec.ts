import { expect, test } from "@playwright/test";

// Real-browser check of the project-entity clustering (#361 Phase 4): sessions resolving to
// one entity merge into ONE cluster labelled by the entity name (across cwds), a
// folder-fallback cluster can be promoted via the header's "Make this a project" button
// (POST /api/projects with {name, folders:[cwd]}), and the toolbar's "+ New project" flow
// creates a standalone entity. Network mocked so the map renders without a backend.

const now = Math.floor(Date.now() / 1000);
const side = { kind: "project", id: "p-1", name: "Side", color: "#5fd7ff" };
const sessions = [
  {
    id: "claude:a",
    engine: "claude",
    uuid: "a",
    short_uuid: "a",
    cwd: "/home/u/app",
    project: side,
    last_mtime: now,
    first_user_message: "",
    title: "App session",
    sticky: false,
    archived: false,
  },
  {
    id: "claude:b",
    engine: "claude",
    uuid: "b",
    short_uuid: "b",
    cwd: "/home/u/lib",
    project: side,
    last_mtime: now - 100,
    first_user_message: "",
    title: "Lib session",
    sticky: false,
    archived: false,
  },
  {
    id: "opencode:c",
    engine: "opencode",
    uuid: "c",
    short_uuid: "c",
    cwd: "/home/u/plain",
    project: { kind: "folder", id: "/home/u/plain", name: "plain" },
    last_mtime: now - 200,
    first_user_message: "",
    title: "Plain session",
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
            side,
            { kind: "folder", id: "/home/u/plain", name: "/home/u/plain" },
          ],
          engines: ["claude", "opencode"],
        },
      },
    }),
  );
  // GET = entity listing (other surfaces may poll it); POST = the creates under test.
  await page.route(/\/api\/projects(\?.*)?$/, (r) => {
    if (r.request().method() === "POST") {
      const body = r.request().postDataJSON() as {
        name: string;
        folders?: string[];
      };
      return r.fulfill({
        json: {
          id: "p-new",
          name: body.name,
          color: "",
          folders: body.folders ?? [],
          archived: false,
          created_at: now,
        },
      });
    }
    return r.fulfill({ json: { projects: [] } });
  });
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({
      json: { folders: [{ cwd: "/home/u/plain", label: "plain" }] },
    }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
});

test("an entity spanning two cwds renders ONE cluster labelled by the entity name", async ({
  page,
}) => {
  await page.goto("/overview");
  const ov = page.locator(".tr-overview");
  await expect(ov).toBeVisible();

  // One merged cluster for the entity (never one per cwd), labelled "Side".
  await expect(ov.getByTitle("Expand Side", { exact: true })).toHaveCount(1);
  await expect(ov.getByText("2 folders", { exact: true })).toBeVisible();
  await expect(
    ov.getByTitle("Expand /home/u/app", { exact: true }),
  ).toHaveCount(0);
  await expect(
    ov.getByTitle("Expand /home/u/lib", { exact: true }),
  ).toHaveCount(0);

  // The unadopted (folder-fallback) session folds into the Default project, not a path-keyed
  // folder node (#445).
  await expect(
    ov.getByTitle("Expand /home/u/plain", { exact: true }),
  ).toHaveCount(0);
  await expect(ov.getByTitle("Expand Default", { exact: true })).toBeVisible();

  // Expanding the entity cluster shows BOTH cwds' chips merged together.
  await ov.getByTitle("Expand Side", { exact: true }).click();
  await expect(
    ov.locator(".tr-ov-chip", { hasText: "App session" }),
  ).toBeVisible();
  await expect(
    ov.locator(".tr-ov-chip", { hasText: "Lib session" }),
  ).toBeVisible();
});

test("'Make this a project' POSTs {name, folders:[cwd]} from a folder node (Folders layout)", async ({
  page,
}) => {
  await page.goto("/overview");
  const ov = page.locator(".tr-overview");
  await expect(ov).toBeVisible();

  // Promote-to-project now lives on folder nodes in the Folders layout (#445): the Projects
  // layout has no folder nodes (unadopted sessions fold into Default).
  await ov.getByRole("radio", { name: /group by folders/i }).click();

  const posted = page.waitForRequest(
    (r) => r.url().includes("/api/projects") && r.method() === "POST",
  );
  await ov.getByTitle("Make ~/plain a project", { exact: true }).click();
  expect((await posted).postDataJSON()).toEqual({
    name: "plain",
    folders: ["/home/u/plain"],
  });
});

test("'+ New project' toolbar flow POSTs a standalone entity (no folders)", async ({
  page,
}) => {
  await page.goto("/overview");
  const ov = page.locator(".tr-overview");
  await expect(ov).toBeVisible();

  await ov.getByTitle("Create a project entity", { exact: true }).click();
  await ov.getByLabel("Project name").fill("Skunkworks");
  const posted = page.waitForRequest(
    (r) => r.url().includes("/api/projects") && r.method() === "POST",
  );
  await ov.getByRole("button", { name: "Create", exact: true }).click();
  expect((await posted).postDataJSON()).toEqual({ name: "Skunkworks" });
});
