import { expect, test } from "@playwright/test";

// Real-browser drag-to-reassign (#424 Phase 5). React Flow node dragging uses pointer/d3-drag
// behaviour that jsdom can't model (the repo rule: drag work needs Playwright), so this proves
// the pointer path end to end: dragging a session chip onto a project cluster PATCHes the
// metadata seam with that entity's project_id. Network is mocked so the map renders backend-free.

const now = Math.floor(Date.now() / 1000);
// A loose (folder-fallback) session to drag, and a session that anchors the "Beta" project
// cluster we drop it onto. Both clusters start expanded so the chips are on screen.
const sessions = [
  {
    id: "claude:drag",
    engine: "claude",
    uuid: "drag",
    short_uuid: "drag",
    cwd: "/home/u/loose",
    project: { kind: "folder", id: "/home/u/loose", name: "/home/u/loose" },
    last_mtime: now,
    first_user_message: "",
    title: "Drag me",
    sticky: false,
    sort_key: 0,
    archived: false,
  },
  {
    id: "claude:anchor",
    engine: "claude",
    uuid: "anchor",
    short_uuid: "anchor",
    cwd: "/home/u/beta",
    project: { kind: "project", id: "p-beta", name: "Beta" },
    last_mtime: now - 100,
    first_user_message: "",
    title: "Anchor",
    sticky: false,
    sort_key: 0,
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
        // Both clusters expanded on load so the chips render without a click.
        overview_expanded: ["/home/u/loose", "project:p-beta"],
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
        facets: { projects: [], engines: ["claude"] },
      },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
});

test("desktop: dragging a session chip onto a project cluster PATCHes its project_id (#424)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "drag-to-reassign is a desktop/pointer path");

  // Capture the assignment write the drop should trigger.
  let patched: { url: string; projectId: unknown } | null = null;
  await page.route("**/api/sessions/*/metadata", async (route) => {
    const body = route.request().postDataJSON() as { project_id?: unknown };
    patched = { url: route.request().url(), projectId: body?.project_id };
    await route.fulfill({ json: { id: "claude:drag", project_id: body?.project_id } });
  });

  await page.goto("/overview");

  // Projects is the default layout → chips are draggable. Grab the React Flow node wrappers by id.
  const chip = page.locator('.react-flow__node[data-id="claude:drag"]');
  const beta = page.locator('.react-flow__node[data-id="group:project:p-beta"]');
  await expect(chip).toBeVisible();
  await expect(beta).toBeVisible();

  const cb = await chip.boundingBox();
  const tb = await beta.boundingBox();
  if (!cb || !tb) throw new Error("missing bounding boxes");

  // Pointer drag: press on the chip, nudge to cross the drag threshold, move onto the Beta
  // cluster, release. getIntersectingNodes resolves the drop target from the rect overlap.
  await page.mouse.move(cb.x + cb.width / 2, cb.y + cb.height / 2);
  await page.mouse.down();
  await page.mouse.move(cb.x + cb.width / 2 + 12, cb.y + cb.height / 2 + 12, { steps: 4 });
  await page.mouse.move(tb.x + tb.width / 2, tb.y + tb.height / 2, { steps: 12 });
  await page.mouse.up();

  await expect.poll(() => patched?.projectId).toBe("p-beta");
  expect(decodeURIComponent(patched!.url)).toContain("/api/sessions/claude:drag/metadata");
});

test("desktop: chips are NOT draggable in Folders/Agents layout (#424)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "desktop path");
  await page.goto("/overview");
  // Switch to Folders — reassignment is project-only, so chips lose their draggable flag.
  await page.locator(".tr-overview").getByRole("radio", { name: /group by folders/i }).click();
  const chip = page.locator('.react-flow__node[data-id="claude:drag"]');
  await expect(chip).toBeVisible();
  await expect(chip).not.toHaveClass(/draggable/);
});
