import { expect, test } from "@playwright/test";

// Real-browser check of the project-entity surfaces (#361 Phase 3): the Settings →
// Projects manager (list / create / archive report) and the new-session project picker
// (owning-entity default + the "none" option). Network is fully mocked — same approach
// as settings-exclude.spec.ts — so both run on the static `vite preview` without a
// backend, on the desktop AND mobile Playwright projects.

type Entity = {
  id: string;
  name: string;
  color: string;
  folders: string[];
  archived: boolean;
  created_at: number;
  session_count: number;
};

const CAYOO: Entity = {
  id: "p-1",
  name: "Cayoo",
  color: "#5fd7ff",
  folders: ["/home/u/cayoo"],
  archived: false,
  created_at: 0,
  session_count: 2,
};

async function mockCommon(page: import("@playwright/test").Page) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        folders: [
          { cwd: "/home/u/cayoo", label: "/home/u/cayoo" },
          { cwd: "/home/u/free", label: "/home/u/free" },
        ],
      },
    }),
  );
}

test.describe("Settings → Projects manager (#361)", () => {
  test.beforeEach(async ({ page }) => {
    await mockCommon(page);
  });

  test("lists a project with its count and folder chip", async ({ page }) => {
    await page.route(/\/api\/projects(\?.*)?$/, (r) =>
      r.fulfill({ json: { projects: [CAYOO] } }),
    );
    await page.goto("/settings/projects");
    await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();
    // Scope to the manager region — the folder-visibility card and the Default
    // project select also render this cwd, so page-wide text lookups are ambiguous.
    const manager = page.getByRole("region", { name: "Projects" });
    await expect(manager.getByText("Cayoo", { exact: true })).toBeVisible();
    await expect(manager.getByText("2 sessions")).toBeVisible();
    await expect(manager.getByTitle("/home/u/cayoo")).toBeVisible();
    // The metadata invariant is part of the panel copy.
    await expect(page.getByText(/never moves session files/i)).toBeVisible();
  });

  test("create flow POSTs the new entity and refetches", async ({ page }) => {
    let created: unknown = null;
    const projects: Entity[] = [CAYOO];
    await page.route(/\/api\/projects(\?.*)?$/, async (r) => {
      if (r.request().method() === "POST") {
        created = r.request().postDataJSON();
        projects.push({
          id: "p-2",
          name: "Fresh",
          color: "",
          folders: [],
          archived: false,
          created_at: 0,
          session_count: 0,
        });
        await r.fulfill({
          json: { id: "p-2", name: "Fresh", color: "", folders: [], archived: false, created_at: 0 },
        });
      } else {
        await r.fulfill({ json: { projects } });
      }
    });
    await page.goto("/settings/projects");
    await page.getByLabel("New project name").fill("Fresh");
    // No folder picked → standalone entity (the "no folder" option).
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await expect.poll(() => created).toEqual({ name: "Fresh", folders: [] });
    // The post-mutation refetch surfaces the new entity.
    await expect(page.getByRole("region", { name: "Projects" }).getByText("Fresh")).toBeVisible();
  });

  test("archive shows the per-member counts from the bulk report", async ({ page }) => {
    await page.route(/\/api\/projects(\?.*)?$/, (r) =>
      r.fulfill({ json: { projects: [CAYOO] } }),
    );
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
    await page.goto("/settings/projects");
    await page.getByRole("button", { name: "Archive project Cayoo" }).click();
    await expect(page.getByText("Archive: 1 archived · 1 already archived · 0 failed")).toBeVisible();
  });
});

test.describe("New-session project picker (#361)", () => {
  test.beforeEach(async ({ page }) => {
    await mockCommon(page);
    await page.route(/\/api\/projects(\?.*)?$/, (r) =>
      r.fulfill({ json: { projects: [CAYOO] } }),
    );
  });

  test("defaults to the owning entity of the selected folder and offers none", async ({
    page,
  }) => {
    await page.goto("/");
    const projectSel = page.getByLabel("Assign to project");
    // /home/u/cayoo (first folder, selected) is adopted by Cayoo → it's the default.
    await expect(projectSel).toHaveValue("p-1");
    await expect(projectSel.locator("option").first()).toHaveText("none (group by folder)");
    // Switching to the un-adopted folder follows: the untouched select falls back to none.
    await page.getByRole("combobox", { name: "Folder" }).selectOption("/home/u/free");
    await expect(projectSel).toHaveValue("");
  });
});
