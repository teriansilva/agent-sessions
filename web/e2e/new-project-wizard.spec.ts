import { expect, type Page, test } from "@playwright/test";
import { mockRoster } from "./roster";
import {
  DASHBOARD_PATH,
  MAP_PATH,
  NEW_PROJECT_PATH,
  SESSIONS_PATH,
} from "../src/lib/routes";

/** The New project wizard (#1187), in a real browser on the desktop AND mobile projects.
 *
 *  What jsdom cannot tell: the rail-vs-progress switch at 800 px, the 44 px targets, overflow at
 *  320 px, the router blocker holding a REAL nav-link click, focus after a real click, and the
 *  New session draft surviving a round trip through two routes (and, on desktop, the map's
 *  "launch back into a window" intent). The network is mocked like projects-settings.spec.ts. */

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
  color: "#ffb000",
  folders: ["/home/u/cayoo"],
  default_folder: "/home/u/cayoo",
  archived: false,
  created_at: 0,
  session_count: 2,
};

interface Mocks {
  created: Record<string, unknown>[];
  mkdirs: Record<string, unknown>[];
  conns: { key: string; launched: boolean }[];
}

async function mockApp(
  page: Page,
  opts: { engines?: string[]; conflict?: boolean; sessions?: unknown[] } = {},
): Promise<Mocks> {
  const m: Mocks = { created: [], mkdirs: [], conns: [] };
  const projects: Entity[] = [CAYOO];
  // Reverse registration order: the catch-all first.
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/**/draft", (r) =>
    r.fulfill({ json: { id: "d", text: "", attachments: [], updated_at: null } }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: opts.engines ?? ["claude", "codex"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: ["project:p-1"],
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
        agent_defaults: { default_engine: "claude", bypass: true },
      },
    }),
  );
  await mockRoster(page);
  const sessions = opts.sessions ?? [];
  await page.route("**/api/sessions?**", (r) =>
    r.fulfill({
      json: {
        sessions,
        next_offset: null,
        total: sessions.length,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, async (r) => {
    if (r.request().method() !== "POST")
      return r.fulfill({ json: { projects } });
    const body = r.request().postDataJSON() as Record<string, string>;
    m.created.push(body);
    if (opts.conflict)
      return r.fulfill({
        status: 409,
        json: {
          detail: `folder '${body.default_folder}' conflicts with '/home/u' already adopted by project p-9`,
        },
      });
    const p: Entity = {
      id: "p-new",
      name: body.name,
      color: body.color ?? "",
      folders: [body.default_folder],
      default_folder: body.default_folder,
      archived: false,
      created_at: 0,
      session_count: 0,
    };
    projects.push(p);
    return r.fulfill({ json: p });
  });
  await page.route(/\/api\/fs\/dirs(\?.*)?$/, (r) => {
    const path = new URL(r.request().url()).searchParams.get("path") || "/home/u";
    if (path === "/home/u")
      return r.fulfill({
        json: {
          path,
          home: "/home/u",
          dirs: [
            { name: "cayoo", path: "/home/u/cayoo" },
            { name: "reuse-me", path: "/home/u/reuse-me" },
            { name: "free", path: "/home/u/free" },
          ],
        },
      });
    return r.fulfill({ json: { path, home: "/home/u", dirs: [] } });
  });
  await page.route("**/api/fs/mkdir", async (r) => {
    const body = r.request().postDataJSON() as { parent: string; name: string };
    m.mkdirs.push(body);
    await r.fulfill({ json: { path: `${body.parent}/${body.name}` } });
  });
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    const u = new URL(ws.url());
    m.conns.push({
      key: decodeURIComponent(u.pathname.replace("/ws/term/", "")),
      launched: u.searchParams.get("new") === "1",
    });
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    ws.send(Buffer.from("\x1b[2J\x1b[Hready\r\n"));
  });
  return m;
}

const heading = (page: Page, name: string) =>
  page.getByRole("heading", { name, exact: true });
const next = (page: Page) => page.getByRole("button", { name: "Next", exact: true }).click();

/** From New session, into the wizard. Waits on the rendered step, never the URL alone. */
async function openFromNewSession(page: Page) {
  await page.getByRole("link", { name: /new project/i }).click();
  await expect(heading(page, "Name the project")).toBeVisible();
}

/** NAME → FOLDER → COLOUR → REVIEW with the suggested new folder. */
async function fillToReview(page: Page, name = "Payments API") {
  await page.getByLabel("Project name").fill(name);
  await next(page);
  await expect(page.getByTestId("np-folder-preview")).toBeVisible();
  await next(page);
  await expect(heading(page, "Pick a colour")).toBeVisible();
  await next(page);
  await expect(heading(page, "Review and create")).toBeVisible();
}

test.describe("New project wizard (#1187)", () => {
  test("the full journey: new folder + colour → the project is selected in New session with its folder", async ({
    page,
  }) => {
    const m = await mockApp(page);
    await page.goto(SESSIONS_PATH);
    await openFromNewSession(page);
    // No session sidebar on the wizard (operator, 2026-09-29): hidden, and no toggle for it.
    await expect(page.locator("aside.sidebar")).toBeHidden();
    await expect(page.locator("header .navToggle")).toHaveCount(0);

    await page.getByLabel("Project name").fill("Payments API");
    await next(page);
    await expect(page.getByTestId("np-folder-preview")).toContainText("~/payments-api");
    await expect(page.getByTestId("np-folder-preview")).toContainText("created at the end");
    await next(page);
    await page.getByRole("button", { name: "Color #c792ea" }).click();
    await expect(page.getByRole("button", { name: "Color #c792ea" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await next(page);
    await expect(page.getByText("created if absent")).toBeVisible();
    expect(m.mkdirs).toEqual([]); // nothing written before CREATE
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(heading(page, "Project created")).toBeVisible();
    expect(m.mkdirs).toEqual([{ parent: "/home/u", name: "payments-api" }]);
    expect(m.created).toEqual([
      { name: "Payments API", color: "#c792ea", default_folder: "/home/u/payments-api" },
    ]);

    await page.getByRole("button", { name: "Start a session" }).click();
    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await expect(page.getByLabel("Project", { exact: true })).toHaveValue("p-new");
    await expect(page.getByLabel("Launch folder")).toHaveValue("/home/u/payments-api");
    await expect(page.locator("aside.sidebar")).not.toBeHidden();
  });

  test("cancel from New session brings back the agent, bypass and an explicit “no project”", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto(SESSIONS_PATH);
    await page.getByLabel("Agent").selectOption("codex");
    await page.getByRole("checkbox", { name: /skip permission/i }).uncheck();
    await page.getByLabel("Project", { exact: true }).selectOption("");
    await openFromNewSession(page);
    await page.getByLabel("Project name").fill("Never mind");
    await page.getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: "Discard and leave" }).click();

    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await expect(page.getByLabel("Agent")).toHaveValue("codex");
    await expect(page.getByRole("checkbox", { name: /skip permission/i })).not.toBeChecked();
    // "" stays "": never swapped for the default project.
    await expect(page.getByLabel("Project", { exact: true })).toHaveValue("");
    expect(page.url()).not.toContain("selectProject");
  });

  test("the browser's Back from the wizard restores New session too", async ({ page }) => {
    await mockApp(page);
    await page.goto(SESSIONS_PATH);
    await page.getByLabel("Agent").selectOption("codex");
    await page.getByRole("checkbox", { name: /skip permission/i }).uncheck();
    await page.getByLabel("Project", { exact: true }).selectOption("");
    await openFromNewSession(page);
    await page.goBack(); // clean draft → no question
    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await expect(page.getByLabel("Agent")).toHaveValue("codex");
    await expect(page.getByRole("checkbox", { name: /skip permission/i })).not.toBeChecked();
    await expect(page.getByLabel("Project", { exact: true })).toHaveValue("");
  });

  test("cancel keeps an UNTOUCHED project untouched and the folder override intact", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto(SESSIONS_PATH);
    await expect(page.getByLabel("Project", { exact: true })).toHaveValue("p-1");
    await page.getByRole("button", { name: /choose folder/i }).click();
    await page.getByRole("button", { name: "Home" }).click(); // the picker opens at ~/cayoo
    await page.getByRole("button", { name: "free" }).click();
    await page.getByRole("button", { name: /^Select/ }).click();
    await expect(page.getByLabel("Launch folder")).toHaveValue("/home/u/free");
    await openFromNewSession(page);
    await page.getByRole("button", { name: "Cancel" }).click(); // clean → no question
    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await expect(page.getByLabel("Project", { exact: true })).toHaveValue("p-1");
    await expect(page.getByLabel("Launch folder")).toHaveValue("/home/u/free");
  });

  test("map → New session → wizard → session: the launch still goes back to the map as a window", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "the map workspace is desktop-only by design");
    test.setTimeout(90_000);
    await page.setViewportSize({ width: 1920, height: 1200 });
    const now = Math.floor(Date.now() / 1000);
    const m = await mockApp(page, {
      sessions: [
        {
          id: "claude:s1",
          engine: "claude",
          uuid: "s1",
          short_uuid: "s1",
          cwd: "/home/u/cayoo",
          project: { kind: "project", id: "p-1", name: "Cayoo", color: "#ffb000" },
          last_mtime: now,
          first_user_message: "",
          title: "Window session 1",
          sticky: false,
          archived: false,
          ai_summary: "",
        },
      ],
    });
    await page.goto(MAP_PATH);
    await expect(page.locator(".tr-overview .tr-ov-chip").first()).toBeVisible();
    await page.locator('.sidebar .sidebarBody a[href="/"]').first().click();
    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await page.getByLabel("Agent").selectOption("codex");
    await page.getByRole("checkbox", { name: /skip permission/i }).uncheck();

    await openFromNewSession(page);
    await fillToReview(page);
    await page.getByRole("button", { name: "Create project" }).click();
    await page.getByRole("button", { name: "Done" }).click();

    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await expect(page.getByLabel("Agent")).toHaveValue("codex");
    await expect(page.getByRole("checkbox", { name: /skip permission/i })).not.toBeChecked();
    await expect(page.getByLabel("Project", { exact: true })).toHaveValue("p-new");
    await page.getByRole("button", { name: /start session/i }).click();
    // Back on the map, as a window — the map-return intent survived the detour.
    await expect(page).toHaveURL(new RegExp(`${MAP_PATH}$`));
    await expect(page.locator("[data-session-window]")).toHaveCount(1);
    await expect.poll(() => m.conns.filter((c) => c.launched).length).toBe(1);
    expect(m.conns.find((c) => c.launched)?.key).toMatch(/^codex:/);
  });

  test("a new folder name that already exists is labelled existing folder, reused", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto(NEW_PROJECT_PATH);
    await page.getByLabel("Project name").fill("Reuse me");
    await next(page);
    await expect(page.getByLabel("Folder name")).toHaveValue("reuse-me");
    await expect(page.getByTestId("np-folder-preview")).toContainText("existing folder, reused");
  });

  test("Back keeps the values; the heading takes focus on every step change", async ({ page }) => {
    await mockApp(page);
    await page.goto(NEW_PROJECT_PATH);
    await page.getByLabel("Project name").fill("Keep");
    await next(page);
    await expect(heading(page, "Where does it live?")).toBeFocused();
    await page.getByLabel("Folder name").fill("kept-folder");
    await next(page);
    await expect(heading(page, "Pick a colour")).toBeFocused();
    await page.getByRole("button", { name: "Back", exact: true }).click();
    await expect(heading(page, "Where does it live?")).toBeFocused();
    await expect(page.getByLabel("Folder name")).toHaveValue("kept-folder");
    await page.getByRole("button", { name: "Back", exact: true }).click();
    await expect(page.getByLabel("Project name")).toHaveValue("Keep");
  });

  test("leaving a dirty draft through the nav asks first", async ({ page }) => {
    await mockApp(page);
    await page.goto(NEW_PROJECT_PATH);
    await page.getByLabel("Project name").fill("Half done");
    // A REAL nav-link click — the router blocker has to hold it.
    await page.locator(`header a[href="${DASHBOARD_PATH}"]`).first().click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("has not been saved");
    await dialog.getByRole("button", { name: "Keep editing" }).click();
    await expect(page).toHaveURL(new RegExp(`${NEW_PROJECT_PATH}$`));
    await expect(page.getByLabel("Project name")).toHaveValue("Half done");
  });

  test("a 409 folder conflict keeps REVIEW with the server's reason and a way back to FOLDER", async ({
    page,
  }) => {
    await mockApp(page, { conflict: true });
    await page.goto(NEW_PROJECT_PATH);
    await fillToReview(page);
    await page.getByRole("button", { name: "Create project" }).click();
    const err = page.getByTestId("np-create-error");
    await expect(err).toContainText("conflicts with '/home/u'");
    await expect(err).toContainText("The folder ~/payments-api exists and stays where it is.");
    await expect(heading(page, "Review and create")).toBeVisible();
    await err.getByRole("button", { name: "Change the folder" }).click();
    await expect(heading(page, "Where does it live?")).toBeVisible();
  });

  test("the dashboard's New project button opens the wizard; Done returns there", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto(DASHBOARD_PATH);
    await page.getByTestId("dashboard-new-project").click();
    await expect(heading(page, "Name the project")).toBeVisible();
    await fillToReview(page, "From the dashboard");
    await page.getByRole("button", { name: "Create project" }).click();
    await page.getByRole("button", { name: "Done" }).click();
    await expect(page.getByTestId("dashboard-page")).toBeVisible();
  });

  test("44 px targets and no horizontal overflow at 320 px, on every step", async ({ page }) => {
    await mockApp(page);
    await page.setViewportSize({ width: 320, height: 720 });
    await page.goto(NEW_PROJECT_PATH);
    await expect(page.getByTestId("wizard-progress")).toBeVisible();
    await expect(page.getByRole("navigation", { name: "New project steps" })).toBeHidden();

    const check = async (label: string) => {
      const over = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(over, `${label}: horizontal overflow`).toBeLessThanOrEqual(0);
      const small = await page.evaluate(() => {
        const main = document.querySelector("#wizard-step-heading")?.closest("section")
          ?.parentElement;
        if (!main) return ["no wizard"];
        return [...main.querySelectorAll<HTMLElement>("button, a, input:not([type=radio]):not([type=checkbox]), label")]
          .filter((el) => el.offsetParent !== null)
          .map((el) => ({ el, r: el.getBoundingClientRect() }))
          .filter(({ r }) => r.height < 44 || r.right > window.innerWidth + 0.5)
          .map(({ el, r }) => `${el.tagName} "${el.textContent?.trim().slice(0, 30)}" ${Math.round(r.height)}px right=${Math.round(r.right)}`);
      });
      expect(small, `${label}: under 44 px or off-screen`).toEqual([]);
    };

    await check("name");
    await page.getByLabel("Project name").fill("A project with a rather long name indeed");
    await next(page);
    await expect(page.getByTestId("np-folder-preview")).toBeVisible();
    await check("folder");
    await next(page);
    await check("colour");
    await next(page);
    await expect(heading(page, "Review and create")).toBeVisible();
    await check("review");
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(heading(page, "Project created")).toBeVisible();
    await check("done");
  });
});
