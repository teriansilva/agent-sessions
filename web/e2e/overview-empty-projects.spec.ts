import { expect, test } from "@playwright/test";

// #447 — empty projects render as clusters in the Projects flowchart so they're drag targets.
// Real-browser proof: a project with zero sessions (from /api/projects) appears as a cluster,
// and dragging a session chip onto it PATCHes the session's project_id. Network mocked.

const now = Math.floor(Date.now() / 1000);
const sessions = [
  {
    id: "claude:s",
    engine: "claude",
    uuid: "s",
    short_uuid: "s",
    cwd: "/home/u/work",
    // folder-fallback → lands in the Default cluster (which we expand so the chip renders)
    project: { kind: "folder", id: "/home/u/work", name: "/home/u/work" },
    last_mtime: now,
    first_user_message: "",
    title: "Drag me",
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
        overview_expanded: ["project:__default__"], // expand Default so the source chip is on screen
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
            { kind: "project", id: "p-empty", name: "Empty", color: "", count: 0 },
            { kind: "project", id: "__default__", name: "Default", color: "", count: 1 },
          ],
          engines: ["claude"],
        },
      },
    }),
  );
  // The empty project entity the canvas fetches (#447): 0 sessions, non-archived.
  await page.route(/\/api\/projects(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        projects: [
          {
            id: "p-empty",
            name: "Empty",
            color: "",
            folders: [],
            archived: false,
            session_count: 0,
          },
        ],
      },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
});

test("an empty project renders as a cluster and accepts a dropped session (#447)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "drag-to-reassign is a desktop/pointer path");

  let patched: { url: string; projectId: unknown } | null = null;
  await page.route("**/api/sessions/*/metadata", async (route) => {
    const body = route.request().postDataJSON() as { project_id?: unknown };
    patched = { url: route.request().url(), projectId: body?.project_id };
    await route.fulfill({ json: { id: "claude:s", project_id: body?.project_id } });
  });

  await page.goto("/overview");
  const ov = page.locator(".tr-overview");
  await expect(ov).toBeVisible();

  // The 0-session project is a visible cluster + a discoverable drop target.
  await expect(ov.getByTitle("Expand Empty", { exact: true })).toBeVisible();
  await expect(ov.getByText("drag sessions here", { exact: true })).toBeVisible();

  // Drag the session chip from Default onto the empty project cluster.
  const chip = page.locator('.react-flow__node[data-id="claude:s"]');
  const empty = page.locator('.react-flow__node[data-id="group:project:p-empty"]');
  await expect(chip).toBeVisible();
  await expect(empty).toBeVisible();
  const cb = await chip.boundingBox();
  const tb = await empty.boundingBox();
  if (!cb || !tb) throw new Error("missing bounding boxes");
  await page.mouse.move(cb.x + cb.width / 2, cb.y + cb.height / 2);
  await page.mouse.down();
  await page.mouse.move(cb.x + cb.width / 2 + 12, cb.y + cb.height / 2 + 12, { steps: 4 });
  await page.mouse.move(tb.x + tb.width / 2, tb.y + tb.height / 2, { steps: 12 });
  await page.mouse.up();

  await expect.poll(() => patched?.projectId).toBe("p-empty");
  expect(decodeURIComponent(patched!.url)).toContain("/api/sessions/claude:s/metadata");
});
