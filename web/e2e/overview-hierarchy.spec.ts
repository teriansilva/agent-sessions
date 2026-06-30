import { expect, test } from "@playwright/test";

// Real-browser check of the hierarchical overview + custom names (#148): nested projects are
// linked by an edge, a custom-named cluster shows its name (with the path subtitle), and the
// header toggle still expands. Network mocked so the map renders without a backend.

const now = Math.floor(Date.now() / 1000);
const sessions = [
  {
    id: "claude:r",
    engine: "claude",
    uuid: "r",
    short_uuid: "r",
    cwd: "/home/u/claude",
    project: { kind: "folder", id: "/home/u/claude", name: "claude" },
    last_mtime: now,
    first_user_message: "",
    title: "Root session",
    sticky: false,
    archived: false,
  },
  {
    id: "claude:c",
    engine: "claude",
    uuid: "c",
    short_uuid: "c",
    cwd: "/home/u/claude/demoapp.io",
    project: { kind: "folder", id: "/home/u/claude/demoapp.io", name: "demoapp.io" },
    last_mtime: now - 100,
    first_user_message: "",
    title: "Child session",
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
        project_names: { "/home/u/claude": "Claude WS" },
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions, next_offset: null, total: 2, facets: { projects: [{ kind: "folder", id: "/home/u/claude", name: "/home/u/claude" }, { kind: "folder", id: "/home/u/claude/demoapp.io", name: "/home/u/claude/demoapp.io" }], engines: ["claude"] } },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
});

test("desktop: nested projects form a tree with a custom name + working toggle (#148)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "covered on desktop");
  await page.goto("/overview");
  const ov = page.locator(".tr-overview");
  await expect(ov).toBeVisible();

  // The cwd tree (nesting + custom names) lives in the Folders layout now (#445): the Projects
  // layout folds unadopted sessions into the Default project and draws no folder nodes.
  await ov.getByRole("radio", { name: /group by folders/i }).click();

  // Custom name shown on the root cluster + its path subtitle.
  await expect(ov.getByText("Claude WS")).toBeVisible();
  await expect(ov.getByText("~/claude", { exact: true })).toBeVisible();

  // One parent→child hierarchy edge is drawn.
  await expect(page.locator(".react-flow__edge")).toHaveCount(1);

  // Header toggle still works (clusters collapsed by default → no chips in the canvas).
  const rootHeader = ov.getByTitle("Expand /home/u/claude", { exact: true });
  await expect(rootHeader).toBeVisible();
  await expect(ov.locator(".tr-ov-chip")).toHaveCount(0);
  await rootHeader.click();
  await expect(ov.locator(".tr-ov-chip", { hasText: "Root session" })).toBeVisible();
});
