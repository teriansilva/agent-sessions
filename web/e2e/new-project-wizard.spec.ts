import { expect, type Page, test } from "@playwright/test";
import { mockRoster } from "./roster";
import { DASHBOARD_PATH, MAP_PATH, NEW_PROJECT_PATH, SESSIONS_PATH, NEW_SESSION_PATH } from "../src/lib/routes";
import { agentPicker, projectPicker, launchFolder } from "./newSessionForm";
import { TOUCH_FLOOR } from "./touchTarget";

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

const SAMPLEPROJECT: Entity = {
  id: "p-1",
  name: "SampleProject",
  color: "#ffb000",
  folders: ["/home/u/sampleproject"],
  default_folder: "/home/u/sampleproject",
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
  const projects: Entity[] = [SAMPLEPROJECT];
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
  await page.route("**/api/playbooks", (r) => r.fulfill({ json: { playbooks: [], default: null, recovery_total: 0 } }));
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
            { name: "sampleproject", path: "/home/u/sampleproject" },
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
  await expect(heading(page, "Start sessions with")).toBeVisible();
  await next(page);
  await expect(heading(page, "Choose a playbook")).toBeVisible();
  await next(page);
  await expect(heading(page, "Review and create")).toBeVisible();
}

test.describe("New project wizard (#1187)", () => {
  test("the colour step offers the sixteen accent colours, all on screen, arrow keys move (#1508)", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto(NEW_PROJECT_PATH);
    await page.getByLabel("Project name").fill("Payments API");
    await next(page);
    await expect(page.getByTestId("np-folder-preview")).toBeVisible();
    await next(page);
    await expect(heading(page, "Pick a colour")).toBeVisible();
    const group = page.getByRole("radiogroup", { name: "Project colour" });
    await expect(group.getByRole("radio")).toHaveCount(17);
    const vw = page.viewportSize()!.width;
    for (const radio of await group.getByRole("radio").all()) {
      const box = (await radio.boundingBox())!;
      expect(box.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      expect(box.x + box.width).toBeLessThanOrEqual(vw);
    }
    // SampleProject is Amber, so Orange is preselected; the arrows walk the wheel.
    const orange = group.getByRole("radio", { name: "Orange" });
    await expect(orange).toHaveAttribute("aria-checked", "true");
    await orange.focus();
    await page.keyboard.press("ArrowRight");
    await expect(group.getByRole("radio", { name: "Signal Red" })).toHaveAttribute("aria-checked", "true");
    await expect(group.getByRole("radio", { name: "Signal Red" })).toBeFocused();
    // Named for touch too, where there is no hover title.
    await expect(page.getByTestId("np-colour-name")).toHaveText(/Selected · Signal Red #c02020/i);
  });

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
    await page.getByRole("radio", { name: "Violet" }).click();
    await expect(page.getByRole("radio", { name: "Violet" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await next(page);
    await next(page);
    await expect(heading(page, "Choose a playbook")).toBeVisible();
    await next(page);
    await expect(page.getByText("created if absent")).toBeVisible();
    expect(m.mkdirs).toEqual([]); // nothing written before CREATE
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(heading(page, "Project created")).toBeVisible();
    expect(m.mkdirs).toEqual([{ parent: "/home/u", name: "payments-api" }]);
    expect(m.created).toEqual([
      { name: "Payments API", color: "#8b5cf6", default_folder: "/home/u/payments-api" },
    ]);

    await page.getByRole("button", { name: "Start a session" }).click();
    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await expect(projectPicker(page).radio("p-new")).toBeChecked();
    await expect(launchFolder(page)).toHaveAttribute("data-cwd", "/home/u/payments-api");
    await expect(page.locator("aside.sidebar")).not.toBeHidden();
  });

  test("cancel from New session brings back the agent, bypass and an explicit “no project”", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto(SESSIONS_PATH);
    await agentPicker(page).selectOption("codex");
    await page.getByRole("checkbox", { name: /skip permission/i }).uncheck();
    await projectPicker(page).selectOption("");
    await openFromNewSession(page);
    await page.getByLabel("Project name").fill("Never mind");
    await page.getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: "Discard and leave" }).click();

    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await expect(agentPicker(page).radio("codex")).toBeChecked();
    await expect(page.getByRole("checkbox", { name: /skip permission/i })).not.toBeChecked();
    // "" stays "": never swapped for the default project.
    await expect(projectPicker(page).radio("")).toBeChecked();
    expect(page.url()).not.toContain("selectProject");
  });

  for (const back of ["cancel", "browser Back"] as const) {
    test(`a chosen specialist survives the detour (${back}) and still launches bound (#1693)`, async ({
      page,
    }) => {
      await mockApp(page);
      await page.route("**/api/specialists", (r) =>
        r.fulfill({
          json: {
            specialists: [
              {
                id: "implementer",
                revision: 1,
                archived: false,
                grade: null,
                live: 0,
                skills_missing: [],
                skills_outdated: [],
                held: [],
                current: {
                  version: 3,
                  title: "Implementer",
                  summary: "",
                  skills: [],
                  engine: "codex",
                  model: "default",
                  effort: null,
                  capacity: 2,
                  created_at: 0,
                },
              },
            ],
            templates: [],
            max_instances: 6,
            live_total: 0,
          },
        }),
      );
      const sockets: URL[] = [];
      await page.routeWebSocket(/\/ws\/term\//, (ws) => {
        sockets.push(new URL(ws.url()));
      });
      await page.goto(SESSIONS_PATH);
      const specialist = page.getByRole("combobox", { name: "Specialist" });
      await specialist.selectOption("implementer");
      await expect(page.getByTestId("new-session-specialist-note")).toBeVisible();
      await openFromNewSession(page);
      if (back === "cancel") await page.getByRole("button", { name: "Cancel" }).click();
      else await page.goBack(); // clean draft → no question
      await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
      await expect(page.getByTestId("new-session-specialist-note")).toBeVisible();
      await expect(specialist).toHaveValue("implementer");
      await expect(agentPicker(page).radio("codex")).toBeChecked();
      await page.getByRole("button", { name: /start session/i }).click();
      await expect.poll(() => sockets.length).toBeGreaterThan(0);
      expect(sockets[0].searchParams.get("specialist")).toBe("implementer");
      expect(sockets[0].searchParams.get("specialist_version")).toBe("3");
    });
  }

  test("the browser's Back from the wizard restores New session too", async ({ page }) => {
    await mockApp(page);
    await page.goto(SESSIONS_PATH);
    await agentPicker(page).selectOption("codex");
    await page.getByRole("checkbox", { name: /skip permission/i }).uncheck();
    await projectPicker(page).selectOption("");
    await openFromNewSession(page);
    await page.goBack(); // clean draft → no question
    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await expect(agentPicker(page).radio("codex")).toBeChecked();
    await expect(page.getByRole("checkbox", { name: /skip permission/i })).not.toBeChecked();
    await expect(projectPicker(page).radio("")).toBeChecked();
  });

  test("cancel keeps an UNTOUCHED project untouched, and its folder with it (#1458)", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto(SESSIONS_PATH);
    await expect(projectPicker(page).radio("p-1")).toBeChecked();
    await expect(launchFolder(page)).toHaveAttribute("data-cwd", "/home/u/sampleproject");
    await openFromNewSession(page);
    await page.getByRole("button", { name: "Cancel" }).click(); // clean → no question
    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await expect(projectPicker(page).radio("p-1")).toBeChecked();
    await expect(launchFolder(page)).toHaveAttribute("data-cwd", "/home/u/sampleproject");
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
          cwd: "/home/u/sampleproject",
          project: { kind: "project", id: "p-1", name: "SampleProject", color: "#ffb000" },
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
    // #1648: from the map the form opens in a modal over it; the wizard is a full page, so the
    // detour leaves the map — and the launch still goes back there as a window.
    await page.locator(`.sidebar .sidebarBody a[href="${NEW_SESSION_PATH}"]`).first().click();
    await expect(page.getByTestId("new-session-modal")).toBeVisible();
    await agentPicker(page).selectOption("codex");
    await page.getByRole("checkbox", { name: /skip permission/i }).uncheck();

    await openFromNewSession(page);
    await fillToReview(page);
    await page.getByRole("button", { name: "Create project" }).click();
    await page.getByRole("button", { name: "Done" }).click();

    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await expect(agentPicker(page).radio("codex")).toBeChecked();
    await expect(page.getByRole("checkbox", { name: /skip permission/i })).not.toBeChecked();
    await expect(projectPicker(page).radio("p-new")).toBeChecked();
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
    await page.getByTestId("dashboard-new").click();
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
    await expect(heading(page, "Start sessions with")).toBeVisible();
    await check("agent");
    await next(page);
    await expect(heading(page, "Choose a playbook")).toBeVisible();
    await check("playbook");
    await next(page);
    await expect(heading(page, "Review and create")).toBeVisible();
    await check("review");
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(heading(page, "Project created")).toBeVisible();
    await check("done");
  });
});

/** The playbook steps laid out to use the page (#1508), with one mocked playbook: three inputs (a
 *  required text with no default, an enum with a default, a secret with a saved global) and three
 *  agent steps (Claude Code with a model, opencode which takes none, Codex). */
const ISSUE_TO_LIVE = {
  id: "issue-to-live",
  ok: true,
  error: null,
  source: "bundled",
  editable: false,
  revision: "r1",
  default: false,
  name: "Issue to live",
  summary: "Plan, implement and ship a change from an issue to a deployed build.",
  flows: [
    {
      id: "main",
      title: "Issue to live",
      steps: [
        { id: "plan", title: "Plan the change", actor: { kind: "agent", engine: "claude", model: "opus" }, after: [], note: false },
        { id: "impl", title: "Implement and validate", actor: { kind: "agent", engine: "opencode" }, after: ["plan"], note: false },
        { id: "ship", title: "Ship", actor: { kind: "agent", engine: "codex" }, after: ["impl"], note: false },
        { id: "audit", title: "Audit", actor: { kind: "agent", engine: "absent-agent", model: "future-model" }, after: ["ship"], note: false },
      ],
    },
  ],
  ships: { materials: 4, runbooks: 1, templates: 0, variables: 4 },
  requires: { binaries: ["git"], connections: [] },
};

async function mockPlaybook(page: Page) {
  await page.route("**/api/playbooks", (r) =>
    r.fulfill({ json: { playbooks: [ISSUE_TO_LIVE], default: null, recovery_total: 0 } }),
  );
  await page.route("**/api/playbooks/authoring/schema", (r) =>
    r.fulfill({ json: { agents: ["claude", "opencode", "codex"] } }),
  );
  await page.route("**/api/playbooks/issue-to-live", (r) =>
    r.fulfill({
      json: {
        ...ISSUE_TO_LIVE,
        recovery_total: 0,
        requires_present: { binaries: { git: true } },
        documents: {
          "playbook.toml": {
            variables: [
              { name: "repo_url", kind: "text", type: "url", required: true },
              { name: "house_rules", kind: "text", type: "text", required: false },
              { name: "deploy_target", kind: "text", type: "enum", choices: ["staging", "prod"], default: "staging", required: true },
              { name: "forge_token", kind: "secret", required: true },
            ],
          },
        },
      },
    }),
  );
  await page.route("**/api/template-variables", (r) =>
    r.fulfill({
      json: {
        variables: [{ name: "forge_token", kind: "secret", set: true, created_at: 0, updated_at: 0, used_by: [] }],
        limits: {},
      },
    }),
  );
}

test.describe("New project wizard — playbook setup (#1508)", () => {
  test("a missing required input is red from the first render, defaults show their value, steps are one table", async ({
    page,
  }, testInfo) => {
    await mockApp(page);
    await mockPlaybook(page);
    await page.goto(NEW_PROJECT_PATH);
    await page.getByLabel("Project name").fill("Payments API");
    await next(page);
    await expect(page.getByTestId("np-folder-preview")).toBeVisible();
    await next(page);
    await next(page);
    await next(page);
    await expect(heading(page, "Choose a playbook")).toBeVisible();
    await page.getByRole("radio", { name: /Issue to live/ }).check();
    await expect(page.getByRole("radio", { name: /Issue to live/ })).toBeChecked();
    await next(page);
    await expect(heading(page, "Set up Issue to live")).toBeVisible();

    // repo_url: required, no default → flagged in place before anything is typed.
    const repo = page.getByTestId("pb-input-repo_url");
    await expect(repo.getByText("Required — this playbook declares no default.")).toBeVisible();
    // forge_token is missing too: a secret has no default and never falls back to a global alone.
    await expect(page.getByTestId("pb-missing-count")).toHaveText(/2 required missing/);
    expect(await repo.evaluate((el) => getComputedStyle(el).borderTopColor)).toBe("rgb(192, 32, 32)");
    const nextBtn = page.getByRole("button", { name: "Next", exact: true });
    await expect(nextBtn).toBeDisabled();
    await expect(page.getByText("Fill 2 required inputs to continue")).toBeVisible();

    // The source pickers name what each choice resolves to.
    await expect(
      page.getByRole("radiogroup", { name: "Source for deploy_target" }).getByRole("radio", { name: "Default · staging" }),
    ).toBeChecked();
    await expect(
      page.getByRole("radiogroup", { name: "Source for forge_token" }).getByRole("radio", { name: "Global · set" }),
    ).toBeVisible();
    await page.getByRole("radiogroup", { name: "Source for forge_token" }).getByRole("radio", { name: "Global · set" }).check();
    await page.getByRole("radiogroup", { name: "Source for repo_url" }).getByRole("radio", { name: "Value" }).check();
    await page.getByLabel("Value for repo_url", { exact: true }).fill("https://git.example.com/acme/site.git");
    await expect(page.getByTestId("pb-missing-count")).toHaveCount(0);
    await expect(nextBtn).toBeEnabled();

    // Steps: agent names, the agent's models with their aliases, and no model picker for opencode.
    await expect(page.getByLabel("Agent for Plan the change")).toHaveValue("claude");
    await expect(page.getByLabel("Agent for Plan the change").locator("option:checked")).toHaveText("Claude Code");
    await expect(page.getByLabel("Model for Plan the change").locator("option", { hasText: "opus · claude-opus-5" })).toHaveCount(1);
    // The playbook stores the alias `opus`: it shows as that model, never as the first option.
    await expect(page.getByLabel("Model for Plan the change")).toHaveValue("claude-opus-5");
    const impl = page.getByTestId("pb-step-main:impl");
    await expect(impl.getByText("Model is set in opencode’s own config")).toBeVisible();
    await expect(page.getByLabel("Model for Implement and validate")).toHaveCount(0);

    // Desktop: a step is one row. Mobile: a card, agent above model, inside the screen.
    const agentBox = (await page.getByLabel("Agent for Plan the change").boundingBox())!;
    const modelBox = (await page.getByLabel("Model for Plan the change").boundingBox())!;
    const vw = page.viewportSize()!.width;
    if (testInfo.project.name === "mobile") {
      expect(modelBox.y).toBeGreaterThan(agentBox.y + agentBox.height - 1);
      expect(modelBox.x + modelBox.width).toBeLessThanOrEqual(vw);
    } else {
      expect(Math.abs(modelBox.y - agentBox.y)).toBeLessThan(4);
      expect(modelBox.x).toBeGreaterThan(agentBox.x + agentBox.width);
    }

    // An agent the roster does not know stays visibly unresolved, its requested model shown.
    await expect(page.getByLabel("Agent for Audit")).toHaveValue("absent-agent");
    await expect(page.getByLabel("Model for Audit")).toHaveValue("future-model");
    await expect(page.getByTestId("pb-step-main:audit").getByText(/This assignment is unresolved/)).toBeVisible();
    await expect(page.getByTestId("pb-step-main:audit").getByText("agent unavailable")).toBeVisible();

    // A `text` input is multiline (the server keeps its newlines): it must reach the review
    // request intact, not flattened by a single-line <input>.
    const reviews: { bindings?: { name: string; value?: string }[] }[] = [];
    await page.route("**/api/playbooks/issue-to-live/review", (r) => {
      reviews.push(r.request().postDataJSON());
      return r.fulfill({ status: 503, json: { detail: "review unavailable in this test" } });
    });
    await page.getByRole("radiogroup", { name: "Source for house_rules" }).getByRole("radio", { name: "Value" }).check();
    const rules = page.getByLabel("Value for house_rules", { exact: true });
    await rules.fill("first line\nsecond line");
    await expect(rules).toHaveValue("first line\nsecond line");

    // Set every step to one agent.
    await page.getByLabel("Agent for every step").selectOption("codex");
    await page.getByRole("button", { name: "Apply", exact: true }).click();
    for (const step of ["Plan the change", "Implement and validate", "Ship", "Audit"])
      await expect(page.getByLabel(`Agent for ${step}`)).toHaveValue("codex");

    await next(page);
    await expect(heading(page, "Review and create")).toBeVisible();
    await expect.poll(() => reviews.length).toBeGreaterThan(0);
    const bound = reviews.at(-1)!.bindings?.find((b) => b.name === "house_rules");
    expect(bound?.value).toBe("first line\nsecond line");
  });
});

/** #1654: a format 5 book — its agent steps name roles, so nothing is assigned at deployment. */
const ROLES_BOOK = {
  ...ISSUE_TO_LIVE,
  id: "roles-book",
  name: "Roles book",
  flows: [
    {
      id: "main",
      title: "Roles",
      steps: [
        { id: "plan", title: "Plan the change", actor: { kind: "agent" }, role: { skills: ["forgejo-pr-flow"], min_grade: "strong" }, after: [], note: false },
        { id: "review", title: "Independent review", actor: { kind: "agent" }, role: { skills: ["code-review"], min_grade: "frontier" }, after: ["plan"], note: false },
        { id: "merge", title: "Confirm and merge", actor: { kind: "operator" }, after: ["review"], note: false },
      ],
    },
  ],
};

test.describe("New project wizard — format 5 roles (#1654)", () => {
  test("a format 5 playbook deploys with its roles and no per-step assignment", async ({ page }) => {
    await mockApp(page);
    await page.route("**/api/playbooks", (r) =>
      r.fulfill({ json: { playbooks: [ROLES_BOOK], default: null, recovery_total: 0 } }),
    );
    await page.route("**/api/playbooks/authoring/schema", (r) => r.fulfill({ json: { agents: ["claude"] } }));
    await page.route("**/api/playbooks/roles-book", (r) =>
      r.fulfill({
        json: {
          ...ROLES_BOOK,
          recovery_total: 0,
          requires_present: { binaries: { git: true } },
          documents: { "playbook.toml": { format: 5, variables: [] } },
        },
      }),
    );
    await page.route("**/api/template-variables", (r) => r.fulfill({ json: { variables: [], limits: {} } }));
    const reviews: { assignments?: Record<string, unknown> }[] = [];
    await page.route("**/api/playbooks/roles-book/review", (r) => {
      reviews.push(r.request().postDataJSON());
      return r.fulfill({
        json: {
          digest: "b".repeat(64),
          deployment_id: "d-roles",
          destination: { path: "/home/operator/payments-api", device: 1, inode: 2 },
          variables: [],
          materials: [],
          changes: [],
          conflicts: [],
          targets: [],
          assignments: [],
          capability_requests: [],
          rituals: [],
          playbook: { id: "roles-book", format: 5, source: "bundled", version: "1.0.0", revision: "r1" },
        },
      });
    });
    await page.goto(NEW_PROJECT_PATH);
    await page.getByLabel("Project name").fill("Payments API");
    for (let i = 0; i < 6 && !(await heading(page, "Choose a playbook").isVisible()); i++) await next(page);
    await expect(heading(page, "Choose a playbook")).toBeVisible();
    await page.getByTestId("playbook-card-roles-book").getByRole("radio").check();
    await next(page);
    const roles = page.locator('section[aria-labelledby="pb-roles-h"]');
    await expect(roles).toContainText("forgejo-pr-flow");
    await expect(roles).toContainText("code-review");
    await expect(roles).toContainText("Senior");
    await expect(roles).toContainText("a specialist, per mission");
    await expect(page.getByRole("heading", { name: "Agents per step", exact: true })).toHaveCount(0);
    await expect(page.getByLabel("Agent for Plan the change")).toHaveCount(0);
    // The phone layout keeps the roles in bounds.
    const box = (await roles.boundingBox())!;
    expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width + 1);
    await next(page);
    await expect(heading(page, "Review and create")).toBeVisible();
    await expect.poll(() => reviews.length).toBeGreaterThan(0);
    for (const body of reviews) expect(body.assignments ?? {}).toEqual({});
    await expect(page.getByRole("heading", { name: "Assignments", exact: true })).toHaveCount(0);
  });
});
