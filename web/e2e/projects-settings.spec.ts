import { expect, test, type Page } from "@playwright/test";
import { projectPicker, launchFolder } from "./newSessionForm";
import { MEMORY_PATH, NEW_SESSION_PATH, PROJECTS_PATH, projectPath } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";

// Real-browser check of HQ → Projects (#2143; it was Settings → Projects, #361/#448): the index (one
// row = one link), a project's General tab (default folder, colour, default project, archive), the
// wizard's round trip, the HQ sub-menu reaching it on desktop and on a phone's sheet, the moved
// Settings URL, the states a read can be in, and the memory switch agreeing with Library → Memory.
// Network is fully mocked — the static `vite preview` runs it on the desktop AND mobile projects.

type Entity = {
  id: string;
  name: string;
  color: string;
  folders: string[];
  default_folder: string;
  archived: boolean;
  created_at: number;
  session_count: number;
};

const SAMPLEPROJECT: Entity = {
  id: "p-1",
  name: "SampleProject",
  color: "#5fd7ff",
  folders: ["/home/u/sampleproject"],
  default_folder: "/home/u/sampleproject",
  archived: false,
  created_at: 0,
  session_count: 2,
};
const OTHER: Entity = {
  ...SAMPLEPROJECT,
  id: "p-2",
  name: "OtherProject",
  color: "#a78bfa",
  folders: ["/home/u/other"],
  default_folder: "/home/u/other",
};

async function mockCommon(page: Page) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [{ cwd: "/home/u/sampleproject", label: "/home/u/sampleproject" }] } }),
  );
  await page.route(/\/api\/playbooks$/, (r) => r.fulfill({ json: { playbooks: [], default: null, recovery_total: 0 } }));
  await page.route(/\/api\/artifacts\/activation/, (r) => r.fulfill({ json: { mode: null } }));
  await page.route(/\/api\/variables$/, (r) => r.fulfill({ json: { variables: [], collections: [] } }));
  await page.route(/\/api\/automations$/, (r) =>
    r.fulfill({ json: { automations: [], loop: { enabled: true, owner: true }, limits: {} } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  // The ~/ folder picker (#448): home with two subdirs; navigating into one returns it as the path.
  await page.route(/\/api\/fs\/dirs(\?.*)?$/, (r) => {
    const path = new URL(r.request().url()).searchParams.get("path");
    if (path === "/home/u/free") return r.fulfill({ json: { path: "/home/u/free", home: "/home/u", dirs: [] } });
    return r.fulfill({
      json: {
        path: "/home/u",
        home: "/home/u",
        dirs: [
          { name: "sampleproject", path: "/home/u/sampleproject" },
          { name: "free", path: "/home/u/free" },
        ],
      },
    });
  });
}

/** A stateful `/api/memory` for the two projects: the list, a project's view, its ingest pass and
 *  the switch. `failNext` refuses the next switch with a 500; `holdNext` holds it until released. */
async function mockMemory(page: Page) {
  const enabled: Record<string, boolean> = { "p-1": true, "p-2": false };
  const ctl = {
    failNext: false,
    hold: null as null | (() => void),
    holdNext: false,
  };
  const view = (pid: string) => ({
    project: { id: pid, name: pid === "p-1" ? "SampleProject" : "OtherProject", color: "" },
    enabled: enabled[pid],
    notes: [],
    pending: [],
    refused: [],
    quarantine: [],
    withheld: { notes: 0, reason: "" },
    instructions: "",
    limits: { note_bytes: 4096, notes: 200, inbox_files: 20 },
    specialist: null,
    notebooks: [],
  });
  await page.route("**/api/memory**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();
    if (path === "/api/memory")
      return route.fulfill({
        json: {
          projects: ["p-1", "p-2"].map((id) => ({
            id,
            name: id === "p-1" ? "SampleProject" : "OtherProject",
            color: "",
            enabled: enabled[id],
            notes: id === "p-1" ? 4 : 0,
            pending: 0,
            quarantined: 0,
            last_write: null,
          })),
        },
      });
    const m = /^\/api\/memory\/(p-\d)(\/(enabled|ingest|import))?$/.exec(path);
    if (!m) return route.fulfill({ json: {} });
    const pid = m[1];
    if (m[3] === "enabled" && method === "PUT") {
      if (ctl.failNext) {
        ctl.failNext = false;
        return route.fulfill({ status: 500, json: { detail: "memory store is read-only" } });
      }
      const want = (req.postDataJSON() as { enabled: boolean }).enabled;
      if (ctl.holdNext) {
        ctl.holdNext = false;
        await new Promise<void>((resolve) => (ctl.hold = resolve));
      }
      enabled[pid] = want;
      return route.fulfill({ json: { enabled: want } });
    }
    if (m[3] === "ingest") return route.fulfill({ json: { notes: 0, quarantined: 0, refused: 0, dropped: 0, view: view(pid) } });
    if (m[3] === "import") return route.fulfill({ json: { sources: [], last: null, held: [] } });
    return route.fulfill({ json: view(pid) });
  });
  return { enabled, ctl };
}

const projectsRoute = (page: Page, projects: Entity[]) =>
  page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects } }));

test.describe("HQ → Projects (#2143; was Settings → Projects, #361/#448)", () => {
  test.beforeEach(async ({ page }) => {
    await mockCommon(page);
    await mockMemory(page);
  });

  test("the index lists a project as one link; its page shows the default folder and folder chip", async ({ page }) => {
    await projectsRoute(page, [SAMPLEPROJECT]);
    await page.goto(PROJECTS_PATH);
    await expect(page.getByRole("heading", { level: 1, name: "Projects" })).toBeVisible();
    await expect(page.getByText("HQ // Projects")).toBeVisible();
    await expect(page.getByText(/never moves session files/i)).toBeVisible();
    const row = page.getByRole("link", { name: /SampleProject/ });
    await expect(row.getByText("4 notes")).toBeVisible();
    await expect(row.getByRole("button")).toHaveCount(0);
    await row.click();
    await expect(page).toHaveURL(/\/projects\/p-1$/);
    await expect(page.getByRole("heading", { level: 1, name: "SampleProject" })).toBeVisible();
    await expect(page.getByTitle("/home/u/sampleproject").first()).toBeVisible();
    await expect(page.getByRole("list", { name: "Folders of SampleProject" })).toContainText("~/sampleproject");
    // No in-page HQ view strip on the Projects pages (#2146).
    await expect(page.getByRole("navigation", { name: "HQ views" })).toHaveCount(0);
  });

  test("a project's tabs switch by pointer and keys; a phone shows short labels, full names, the touch floor", async ({ page, isMobile }) => {
    await projectsRoute(page, [SAMPLEPROJECT]);
    // A short viewport: the page is taller than the pane, which once squeezed the tab row to nothing.
    await page.setViewportSize({ width: page.viewportSize()!.width, height: 520 });
    await page.goto(projectPath("p-1"));
    const tabs = page.getByRole("tablist", { name: "SampleProject settings" });
    for (const [name, short, heading] of [
      ["Session defaults", "Defaults", "Agent for new sessions"],
      ["Memory & equipment", "Memory", "Project memory"],
      ["General", "General", "Identity"],
    ] as const) {
      const tab = tabs.getByRole("tab", { name, exact: true });
      await tab.click();
      await expect(tab).toHaveAttribute("aria-selected", "true");
      await expect(page.getByRole("heading", { name: new RegExp(heading) })).toBeVisible();
      if (isMobile) {
        // The short label is what shows; the full name (clipped, not removed) stays the tab's name.
        const shown = tab.locator('[aria-hidden="true"]');
        await expect(shown).toHaveText(short);
        await expect(shown).toBeVisible();
        expect((await tab.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      }
    }
    await expect(page).toHaveURL(/\/projects\/p-1$/);
    // Arrow keys move and select (the Tabs primitive); the URL keeps the tab.
    await tabs.getByRole("tab", { name: "General", exact: true }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(tabs.getByRole("tab", { name: "Session defaults", exact: true })).toBeFocused();
    await expect(page).toHaveURL(/\?tab=defaults$/);
    // Nothing on the page scrolls sideways.
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test("the HQ sub-menu reaches Projects (a sheet on a phone); its items keep the touch floor", async ({ page, isMobile }) => {
    await projectsRoute(page, [SAMPLEPROJECT]);
    await page.goto("/mission");
    await page.locator(".hud-topbar").getByRole("button", { name: "HQ menu" }).click();
    const panel = page.getByTestId("section-menu-mission-panel");
    if (isMobile) await expect(panel).toHaveAttribute("data-sheet", "");
    const item = page.getByRole("menu", { name: "HQ menu" }).getByRole("menuitem", { name: "Projects", exact: true });
    await expect(item).toHaveAttribute("href", PROJECTS_PATH);
    if (isMobile) expect((await item.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await item.click();
    await expect(page).toHaveURL(/\/projects$/);
    await expect(page.getByTestId("projects-index")).toBeVisible();
    // HQ stays the lit section on a page outside `/hq`.
    await expect(page.locator(".hud-topbar").getByRole("link", { name: /HQ/ }).first()).toHaveAttribute("aria-current", /page|true/);
  });

  test("the old Settings URL lands on HQ → Projects, and Settings no longer lists Projects", async ({ page }) => {
    await projectsRoute(page, [SAMPLEPROJECT]);
    await page.goto("/settings/projects");
    await expect(page).toHaveURL(/\/projects$/);
    await expect(page.getByTestId("projects-index")).toBeVisible();
    await page.goto("/settings/projects#discovery-h");
    await expect(page).toHaveURL(/\/projects\?tab=folders$/);
    await expect(page.getByRole("heading", { name: /project roots/i })).toBeVisible();
  });

  test("a failed list read is red with Retry — never 'No projects yet' — and Retry recovers", async ({ page }) => {
    let fail = true;
    await page.route(/\/api\/projects(\?.*)?$/, (r) =>
      fail ? r.fulfill({ status: 503, json: { detail: "Service unavailable" } }) : r.fulfill({ json: { projects: [SAMPLEPROJECT] } }),
    );
    await page.goto(PROJECTS_PATH);
    const failed = page.getByTestId("projects-read-failed");
    await expect(failed).toContainText("Couldn’t load projects");
    await expect(page.getByTestId("projects-empty")).toHaveCount(0);
    fail = false;
    await failed.getByRole("button", { name: /retry/i }).click();
    await expect(page.getByRole("link", { name: /SampleProject/ })).toBeVisible();
  });

  test("the colour picker offers the sixteen accent colours and keeps an old colour as Current (#1508)", async ({ page }) => {
    const patches: unknown[] = [];
    await projectsRoute(page, [SAMPLEPROJECT]);
    await page.route("**/api/projects/p-1", (r) => {
      patches.push(r.request().postDataJSON());
      return r.fulfill({ json: { ...SAMPLEPROJECT, color: "#38bdf8" } });
    });
    await page.goto(projectPath("p-1"));
    const group = page.getByRole("radiogroup", { name: "Color for SampleProject" });
    // #5fd7ff was one of the old six; it is not among the sixteen, so it leads, checked.
    const current = group.getByRole("radio", { name: "Current colour #5fd7ff" });
    await expect(current).toHaveAttribute("aria-checked", "true");
    await expect(group.getByRole("radio")).toHaveCount(18);
    for (const name of ["Amber", "Signal Red", "Violet", "Sky", "Tactical Green", "Ice", "None"]) {
      const radio = group.getByRole("radio", { name, exact: true });
      await expect(radio).toBeVisible();
      const box = (await radio.boundingBox())!;
      expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width);
    }
    await group.getByRole("radio", { name: "Sky" }).click();
    await expect.poll(() => patches).toEqual([{ color: "#38bdf8" }]);
  });

  test("the default-project switch persists default_project_id, and clears it (#615)", async ({ page }) => {
    let saved: unknown = null;
    await page.route("**/api/prefs", async (r) => {
      saved = r.request().postDataJSON();
      await r.fulfill({ json: saved });
    });
    await projectsRoute(page, [SAMPLEPROJECT]);
    await page.goto(projectPath("p-1"));
    const sw = page.getByRole("switch", { name: "Default project" });
    await expect(sw).toHaveAttribute("aria-checked", "false");
    await sw.click();
    await expect.poll(() => saved).toEqual({ default_project_id: "p-1" });
    await expect(sw).toHaveAttribute("aria-checked", "true");
    await sw.click();
    await expect.poll(() => saved).toEqual({ default_project_id: "" });
    await expect(sw).toHaveAttribute("aria-checked", "false");
  });

  test("New project opens the wizard; Done comes back to the index (#1187)", async ({ page }) => {
    let created: unknown = null;
    const projects: Entity[] = [SAMPLEPROJECT];
    await page.route(/\/api\/projects(\?.*)?$/, async (r) => {
      if (r.request().method() !== "POST") return r.fulfill({ json: { projects } });
      created = r.request().postDataJSON();
      const p: Entity = { ...SAMPLEPROJECT, id: "p-2", name: "Fresh", color: "", folders: ["/home/u/free"], default_folder: "/home/u/free", session_count: 0 };
      projects.push(p);
      return r.fulfill({ json: p });
    });
    await page.goto(PROJECTS_PATH);
    await page.getByRole("link", { name: /new project/i }).first().click();
    await expect(page.getByRole("heading", { name: "Name the project" })).toBeVisible();
    await page.getByLabel("Project name").fill("Fresh");
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await page.getByRole("radio", { name: /existing folder/i }).check();
    await page.getByRole("button", { name: "Choose folder…" }).click();
    await page.getByRole("button", { name: "free" }).click();
    await page.getByRole("button", { name: /^Select/ }).click();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await page.getByRole("radio", { name: "None", exact: true }).click();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await page.getByRole("button", { name: "Next", exact: true }).click(); // session defaults: none
    await expect(page.getByRole("heading", { name: "Choose a playbook", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await page.getByRole("button", { name: "Create project" }).click();
    await expect.poll(() => created).toEqual({ name: "Fresh", color: "", default_folder: "/home/u/free" });
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await expect(page).toHaveURL(/\/projects$/);
    await expect(page.getByRole("link", { name: /Fresh/ })).toBeVisible();
  });

  test("archive shows the per-member counts from the bulk report", async ({ page }) => {
    await projectsRoute(page, [SAMPLEPROJECT]);
    await page.route("**/api/projects/p-1/archive", (r) =>
      r.fulfill({
        json: {
          id: "p-1",
          archived: true,
          sessions: [
            { id: "claude:aaaa", result: "archived" },
            { id: "claude:bbbb", result: "already_archived" },
          ],
          counts: { archived: 1, already_archived: 1, failed: 0 },
        },
      }),
    );
    await page.goto(projectPath("p-1"));
    await page.getByRole("button", { name: "Archive project SampleProject" }).click();
    await expect(page.getByText("Archive: 1 archived · 1 already archived · 0 failed")).toBeVisible();
  });
});

test.describe("The memory switch across surfaces (#2143)", () => {
  test.beforeEach(async ({ page }) => {
    await mockCommon(page);
    await projectsRoute(page, [SAMPLEPROJECT, OTHER]);
  });

  test("toggled on Projects, Library → Memory shows it; back on Projects it is still the saved value", async ({ page }) => {
    const { enabled } = await mockMemory(page);
    await page.goto(projectPath("p-1", "memory"));
    const sw = page.getByRole("switch", { name: "Shared memory for SampleProject" });
    await expect(sw).toHaveAttribute("aria-checked", "true");
    await sw.click();
    await expect(sw).toHaveAttribute("aria-checked", "false");
    await expect.poll(() => enabled["p-1"]).toBe(false);
    // The notebook link opens Library → Memory on this project: its switch agrees.
    await page.getByRole("link", { name: /Open notebook in Library → Memory/ }).click();
    await expect(page).toHaveURL(new RegExp(`${MEMORY_PATH}\\?project=p-1$`));
    await expect(page.getByRole("switch", { name: "Shared memory for SampleProject" })).toHaveAttribute("aria-checked", "false");
    // Back on the project page, a fresh mount reads the same saved value.
    await page.goBack();
    await expect(page.getByRole("switch", { name: "Shared memory for SampleProject" })).toHaveAttribute("aria-checked", "false");
  });

  test("a failed save goes back to the stored value and says why", async ({ page }) => {
    const { ctl } = await mockMemory(page);
    await page.goto(projectPath("p-1", "memory"));
    const sw = page.getByRole("switch", { name: "Shared memory for SampleProject" });
    await expect(sw).toHaveAttribute("aria-checked", "true");
    ctl.failNext = true;
    await sw.click();
    await expect(page.getByTestId("project-memory-note")).toContainText("memory store is read-only");
    await expect(sw).toHaveAttribute("aria-checked", "true");
  });

  test("a late answer for a project you have left never lands on the one you are on", async ({ page }) => {
    const { ctl, enabled } = await mockMemory(page);
    await page.goto(projectPath("p-1", "memory"));
    const sw1 = page.getByRole("switch", { name: "Shared memory for SampleProject" });
    await expect(sw1).toHaveAttribute("aria-checked", "true");
    ctl.holdNext = true;
    await sw1.click();
    await expect.poll(() => ctl.hold !== null).toBe(true);
    // Leave for the other project while p-1's write is still out — inside the app, so the write is
    // not aborted by a page load.
    await page.getByRole("link", { name: "All projects" }).click();
    await page.getByRole("link", { name: /OtherProject/ }).click();
    await page.getByRole("tab", { name: "Memory & equipment" }).click();
    const sw2 = page.getByRole("switch", { name: "Shared memory for OtherProject" });
    await expect(sw2).toHaveAttribute("aria-checked", "false");
    ctl.hold!();
    await expect.poll(() => enabled["p-1"]).toBe(false);
    // p-2 is untouched, and stays what its own read said.
    await expect(sw2).toHaveAttribute("aria-checked", "false");
    await expect(page.getByTestId("project-memory-note")).toHaveCount(0);
  });
});

test.describe("New-session Project → Folder (#448)", () => {
  test.beforeEach(async ({ page }) => {
    await mockCommon(page);
    await projectsRoute(page, [SAMPLEPROJECT]);
  });

  test("the selected project resolves its default folder (#448, #1458)", async ({ page }) => {
    await page.goto(NEW_SESSION_PATH);
    await expect(projectPicker(page).radio("p-1")).toBeChecked(); // SampleProject is default
    await expect(launchFolder(page)).toHaveAttribute("data-cwd", "/home/u/sampleproject"); // its default folder
  });
});
