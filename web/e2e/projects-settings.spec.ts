import { expect, test } from "@playwright/test";

// Real-browser check of the project-entity surfaces: the Settings → Projects manager (list /
// create-with-default-folder / archive report) and the new-session Project→Folder flow (#448:
// the project drives the default launch folder, overridable via the ~/ folder picker). Network is
// fully mocked — same approach as settings-exclude.spec.ts — so both run on the static
// `vite preview` without a backend, on the desktop AND mobile Playwright projects.

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

const CAYOO: Entity = {
  id: "p-1",
  name: "Cayoo",
  color: "#5fd7ff",
  folders: ["/home/u/cayoo"],
  default_folder: "/home/u/cayoo",
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
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({
      json: { folders: [{ cwd: "/home/u/cayoo", label: "/home/u/cayoo" }] },
    }),
  );
  // The ~/ folder picker (#448): home with two subdirs; navigating into one returns it as the path.
  await page.route(/\/api\/fs\/dirs(\?.*)?$/, (r) => {
    const path = new URL(r.request().url()).searchParams.get("path");
    if (path === "/home/u/free")
      return r.fulfill({
        json: { path: "/home/u/free", home: "/home/u", dirs: [] },
      });
    return r.fulfill({
      json: {
        path: "/home/u",
        home: "/home/u",
        dirs: [
          { name: "cayoo", path: "/home/u/cayoo" },
          { name: "free", path: "/home/u/free" },
        ],
      },
    });
  });
}

test.describe("Settings → Projects manager (#361/#448)", () => {
  test.beforeEach(async ({ page }) => {
    await mockCommon(page);
  });

  test("lists a project with its count, default folder, and folder chip", async ({
    page,
  }) => {
    await page.route(/\/api\/projects(\?.*)?$/, (r) =>
      r.fulfill({ json: { projects: [CAYOO] } }),
    );
    await page.goto("/settings/projects");
    await expect(
      page.getByRole("heading", { name: "Projects", exact: true }),
    ).toBeVisible();
    const manager = page.getByRole("region", { name: "Projects" });
    await expect(manager.getByText("Cayoo", { exact: true })).toBeVisible();
    await expect(manager.getByText("2 sessions")).toBeVisible();
    await expect(manager.getByText(/default folder:/i)).toBeVisible();
    // ~/cayoo shows as both the default-folder path and the adopted-folder chip (#448).
    await expect(manager.getByTitle("/home/u/cayoo").first()).toBeVisible();
    await expect(page.getByText(/never moves session files/i)).toBeVisible();
  });

  test("star: setting the default project persists default_project_id (#615)", async ({
    page,
  }) => {
    // Real browser, not jsdom: this is a click on an icon button whose pressed state and
    // persisted write are the whole feature.
    let saved: unknown = null;
    await page.route("**/api/prefs", async (r) => {
      saved = r.request().postDataJSON();
      await r.fulfill({ json: saved });
    });
    await page.route(/\/api\/projects(\?.*)?$/, (r) =>
      r.fulfill({ json: { projects: [CAYOO] } }),
    );
    await page.goto("/settings/projects");

    const star = page.getByRole("button", {
      name: "Make Cayoo the default project",
    });
    await expect(star).toHaveAttribute("aria-pressed", "false");
    await star.click();
    await expect.poll(() => saved).toEqual({ default_project_id: "p-1" });
    // The row now reads as state, not as an available action.
    await expect(
      page.getByRole("button", {
        name: "Cayoo is the default project — clear it",
      }),
    ).toHaveAttribute("aria-pressed", "true");

    // Clicking the starred project clears it — New Session falls back to the first project.
    await page
      .getByRole("button", { name: "Cayoo is the default project — clear it" })
      .click();
    await expect.poll(() => saved).toEqual({ default_project_id: "" });
    await expect(
      page.getByRole("button", { name: "Make Cayoo the default project" }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  test("the retired Default project card is gone (#615)", async ({ page }) => {
    await page.route(/\/api\/projects(\?.*)?$/, (r) =>
      r.fulfill({ json: { projects: [CAYOO] } }),
    );
    await page.goto("/settings/projects");
    await expect(
      page.getByRole("heading", { name: "Projects", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("combobox", { name: "Default project" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: "Default project" }),
    ).toHaveCount(0);
  });

  test("create requires a default folder (picker), POSTs it, and refetches (#448)", async ({
    page,
  }) => {
    let created: unknown = null;
    const projects: Entity[] = [CAYOO];
    await page.route(/\/api\/projects(\?.*)?$/, async (r) => {
      if (r.request().method() === "POST") {
        created = r.request().postDataJSON();
        projects.push({
          id: "p-2",
          name: "Fresh",
          color: "",
          folders: ["/home/u/free"],
          default_folder: "/home/u/free",
          archived: false,
          created_at: 0,
          session_count: 0,
        });
        await r.fulfill({
          json: {
            id: "p-2",
            name: "Fresh",
            color: "",
            folders: ["/home/u/free"],
            default_folder: "/home/u/free",
            archived: false,
            created_at: 0,
          },
        });
      } else {
        await r.fulfill({ json: { projects } });
      }
    });
    await page.goto("/settings/projects");
    await page.getByLabel("New project name").fill("Fresh");
    // A default folder is required → Create stays disabled until one is picked.
    await expect(
      page.getByRole("button", { name: "Create", exact: true }),
    ).toBeDisabled();
    await page
      .getByRole("button", { name: "Choose the default folder" })
      .click();
    await page.getByRole("button", { name: "free" }).click(); // navigate into ~/free
    await page.getByRole("button", { name: /^Select/ }).click(); // pick ~/free
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await expect
      .poll(() => created)
      .toEqual({ name: "Fresh", default_folder: "/home/u/free" });
    await expect(
      page.getByRole("region", { name: "Projects" }).getByText("Fresh"),
    ).toBeVisible();
  });

  test("archive shows the per-member counts from the bulk report", async ({
    page,
  }) => {
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
    await expect(
      page.getByText("Archive: 1 archived · 1 already archived · 0 failed"),
    ).toBeVisible();
  });
});

test.describe("New-session Project → Folder (#448)", () => {
  test.beforeEach(async ({ page }) => {
    await mockCommon(page);
    await page.route(/\/api\/projects(\?.*)?$/, (r) =>
      r.fulfill({ json: { projects: [CAYOO] } }),
    );
  });

  test("the selected project prefills the folder; the picker overrides it", async ({
    page,
  }) => {
    await page.goto("/");
    await expect(page.getByLabel("Project", { exact: true })).toHaveValue(
      "p-1",
    ); // Cayoo is default
    await expect(page.getByLabel("Launch folder")).toHaveValue("/home/u/cayoo"); // its default folder

    // Override the folder for this session via the ~/ picker.
    await page.getByRole("button", { name: /choose folder/i }).click();
    await page.getByRole("button", { name: "free" }).click();
    await page.getByRole("button", { name: /^Select/ }).click();
    await expect(page.getByLabel("Launch folder")).toHaveValue("/home/u/free");
  });
});
