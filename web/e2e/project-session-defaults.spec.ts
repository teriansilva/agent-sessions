import { expect, type Page, test } from "@playwright/test";
import { ROSTER } from "./roster";
import { NEW_PROJECT_PATH } from "../src/lib/routes";
import { settingsPath } from "../src/routes/settingsTabs";
import { agentPicker, projectPicker } from "./newSessionForm";

// Project session defaults (#1508) in a real browser: a project names the agent, model, effort and
// permission choice its new sessions start with. The New session form preselects them — the
// operator's own pick on the form always wins, the global defaults fill the rest — and a project's
// "skip" never applies before the project list is confirmed. The wizard's Agent step and the
// Settings → Projects editor write the block. Network fully mocked.

const API = "opencode-api";
const MODELS = {
  status: "ok",
  reason: null,
  models: [
    { id: "local/coder", label: "Coder", description: null, efforts: ["low", "high"], is_default: false },
    { id: "openai/plain", label: "Plain", description: null, efforts: [], is_default: false },
  ],
};

type Entity = {
  id: string;
  name: string;
  color: string;
  folders: string[];
  default_folder: string;
  archived: boolean;
  created_at: number;
  session_count: number;
  launch_defaults?: Record<string, unknown>;
};
const project = (id: string, name: string, launch_defaults?: Record<string, unknown>): Entity => ({
  id,
  name,
  color: "#38bdf8",
  folders: [`/home/u/${id}`],
  default_folder: `/home/u/${id}`,
  archived: false,
  created_at: 0,
  session_count: 0,
  ...(launch_defaults ? { launch_defaults } : {}),
});

async function mockApp(
  page: Page,
  opts: {
    projects: Entity[];
    bypass?: boolean;
    overrides?: Record<string, Record<string, unknown>>;
  },
) {
  const engines = ROSTER.engines.map((e) => ({
    ...(e.runtime === "api" ? { ...e, present: true, supports_new: true } : e),
    ...(opts.overrides?.[e.id] ?? {}),
  }));
  const ids = engines.filter((e) => e.present).map((e) => e.id);
  const state = {
    projects: opts.projects,
    creates: [] as Record<string, unknown>[],
    created: [] as Record<string, unknown>[],
    patches: [] as Record<string, unknown>[],
    /** Set to hold the NEXT `/api/projects?counts=0` read until released. */
    hold: null as null | Promise<void>,
    /** Set to fail the NEXT `/api/projects?counts=0` read (a 500). */
    failNext: false,
  };
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    if (path === "/api/config") {
      return json({
        csrf: "x",
        new_session_engines: ids,
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: "",
        agent_defaults: { default_engine: "claude", bypass: opts.bypass ?? true, session_defaults: {} },
      });
    }
    if (path === "/api/engines") return json({ engines, problems: [] });
    if (path === `/api/structured/clients/${API}/models`) return json(MODELS);
    if (path.startsWith("/api/structured/clients/")) return json({ status: "ok", reason: null, models: [] });
    if (path === "/api/structured/sessions" && req.method() === "POST") {
      state.creates.push(req.postDataJSON());
      return json({ session_key: `${API}:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f` }, 201);
    }
    if (path === "/api/projects" && req.method() === "POST") {
      const body = req.postDataJSON();
      state.created.push(body);
      const p = { ...project("p-new", body.name, body.launch_defaults), default_folder: body.default_folder };
      state.projects = [...state.projects, p];
      return json(p);
    }
    if (path.startsWith("/api/projects/") && req.method() === "PATCH") {
      const id = path.split("/").pop()!;
      const body = req.postDataJSON();
      state.patches.push(body);
      state.projects = state.projects.map((p) => (p.id === id ? { ...p, ...body } : p));
      return json(state.projects.find((p) => p.id === id));
    }
    if (path === "/api/projects") {
      if (url.searchParams.get("counts") === "0" && state.failNext) {
        state.failNext = false;
        return json({ detail: "projects store unavailable" }, 500);
      }
      if (url.searchParams.get("counts") === "0" && state.hold) {
        const held = state.hold;
        state.hold = null;
        await held;
      }
      return json({ projects: state.projects });
    }
    if (path === "/api/fs/dirs") return json({ path: "/home/u", home: "/home/u", dirs: [] });
    if (path === "/api/fs/mkdir") return json({ path: "/home/u/x" });
    if (path === "/api/playbooks") return json({ playbooks: [], default: null, recovery_total: 0 });
    if (path === "/api/version") return json({ version: "test" });
    if (path === "/api/pulse/notifications") return json({ notifications: [], unread: 0, uncertain: 0, settled: [] });
    if (path.startsWith("/api/missions")) return json({ missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } });
    if (path.startsWith("/api/sessions")) return json({ sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } });
    if (path.startsWith("/api/folders")) return json({ folders: [] });
    return json({});
  });
  return state;
}

const tile = (page: Page, engine: string) => page.locator(`label[data-engine="${engine}"]`);
const agentChecked = (page: Page, engine: string) => agentPicker(page).radio(engine);
const chooseProject = async (page: Page, id: string) => {
  await projectPicker(page).selectOption(id);
  await expect(projectPicker(page).radio(id)).toBeChecked();
};
const skip = (page: Page) => page.getByRole("checkbox", { name: /Skip permission prompts/ });
/** SPA navigation without a reload, so the project picker's cache survives (#1436). */
const spaGo = (page: Page, to: string) =>
  page.evaluate((path) => {
    window.history.pushState({}, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, to);

const WEBSITE = project("p-web", "Website", { engine: API, model: "local/coder", effort: "high", bypass: false });
const PLAIN = project("p-plain", "Plain");
const TERMINAL = project("p-term", "Terminal", { engine: "codex", model: "gpt-5-codex" });

test.describe("New session follows the selected project's defaults (#1508)", () => {
  test("project over global, the operator's own pick over the project, and the create carries it", async ({ page }) => {
    const state = await mockApp(page, { projects: [PLAIN, TERMINAL, WEBSITE] });
    await page.goto("/");

    // Plain sets nothing: the global defaults (claude, skip) — no project badges.
    await chooseProject(page, "p-plain");
    await expect(agentChecked(page, "claude")).toBeChecked();
    await expect(skip(page)).toBeChecked();
    await expect(page.getByTestId("project-badge")).toHaveCount(0);
    await expect(page.getByTestId("new-session-project-defaults")).toHaveCount(0);

    // Website: its API client, model, effort and "ask" — each badged, and the project line says so.
    await chooseProject(page, "p-web");
    await expect(agentChecked(page, API)).toBeChecked();
    await expect(tile(page, API)).toContainText("project");
    await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue("local/coder");
    await expect(page.getByTestId("new-session-effort")).toHaveValue("high");
    await expect(skip(page)).not.toBeChecked();
    await expect(page.getByTestId("project-badge")).toHaveCount(3); // model, effort, permissions
    await expect(page.getByTestId("new-session-project-defaults")).toContainText("Website starts as");
    // Reachable, not hidden under the pane's sticky Start footer: scrolled to, its middle is itself.
    const line = page.getByTestId("new-session-project-defaults");
    await line.scrollIntoViewIfNeeded();
    expect(
      await line.evaluate((el) => {
        const r = el.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return !!hit && el.contains(hit);
      }),
    ).toBe(true);

    // Terminal: a terminal agent's model from the roster; permissions fall back to the global skip.
    await chooseProject(page, "p-term");
    await expect(agentChecked(page, "codex")).toBeChecked();
    await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue("gpt-5-codex");
    await expect(skip(page)).toBeChecked();
    await expect(page.getByTestId("project-badge")).toHaveCount(1); // model only

    // The operator's own agent survives a project switch.
    await agentPicker(page).selectOption("gemini");
    await chooseProject(page, "p-web");
    await expect(agentChecked(page, "gemini")).toBeChecked();

    // Back to the project's agent, and Start: the create carries exactly what the form shows.
    await agentPicker(page).selectOption(API);
    await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue("local/coder");
    await page.getByRole("button", { name: /start session/i }).click();
    await expect.poll(() => state.creates.length).toBe(1);
    expect(state.creates[0]).toMatchObject({ engine: API, model: "local/coder", effort: "high" });
    expect(state.creates[0].bypass ?? false).toBe(false);
  });

  test("a project's skip waits for the confirmed project list; an agent that cannot skip never does", async ({ page }) => {
    const ALWAYS_SKIP = project("p-skip", "Always skip", { engine: API, bypass: true });
    const NO_SKIP = project("p-noskip", "No skip agent", { engine: "codex-api", bypass: true });
    const state = await mockApp(page, {
      projects: [ALWAYS_SKIP, NO_SKIP],
      bypass: false,
      overrides: { "codex-api": { api: { kind: "codex-app-server", source: "codex", can_bypass: false } } },
    });
    await page.goto("/");
    await chooseProject(page, "p-skip");
    await expect(skip(page)).toBeChecked(); // confirmed list: the project's skip applies
    await expect(page.getByTestId("project-badge")).toHaveCount(1);

    // Leave and come back inside the app: the cached list paints at once while it revalidates.
    // Until that settles, the project's skip does NOT apply (the global "ask" does) and Start waits.
    let release!: () => void;
    state.hold = new Promise<void>((r) => (release = r));
    await spaGo(page, settingsPath("projects"));
    await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();
    await spaGo(page, "/");
    await expect(agentChecked(page, API)).toBeChecked();
    await expect(skip(page)).not.toBeChecked();
    await expect(page.getByRole("button", { name: /start session/i })).toBeDisabled();
    release();
    await expect(skip(page)).toBeChecked();

    // A FAILED revalidation is no confirmation either: the cached list stays and Start is allowed,
    // so the project's skip must NOT apply — the global "ask" does, and the create carries no skip.
    state.failNext = true;
    await spaGo(page, settingsPath("projects"));
    await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();
    await spaGo(page, "/");
    await expect(page.getByRole("button", { name: /start session/i })).toBeEnabled();
    await expect(agentChecked(page, API)).toBeChecked();
    await expect(skip(page)).not.toBeChecked();
    await expect(page.getByTestId("project-badge")).toHaveCount(0);
    await page.getByRole("button", { name: /start session/i }).click();
    await expect.poll(() => state.creates.length).toBe(1);
    expect(state.creates[0]).toMatchObject({ engine: API });
    expect(state.creates[0].bypass ?? false).toBe(false);
    await page.goto("/"); // a fresh, confirmed load for the next case

    // A client whose adapter cannot skip: no checkbox, and the create never asks for a skip.
    await chooseProject(page, "p-noskip");
    await expect(agentChecked(page, "codex-api")).toBeChecked();
    await expect(skip(page)).toHaveCount(0);
  });
});

test.describe("Setting a project's session defaults (#1508)", () => {
  test("the wizard's Agent step: model waits for an agent, the review names it, the create carries it", async ({ page }, testInfo) => {
    const state = await mockApp(page, { projects: [PLAIN] });
    await page.goto(NEW_PROJECT_PATH);
    await page.getByLabel("Project name").fill("Payments API");
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Start sessions with" })).toBeVisible();

    const editor = page.getByTestId("project-session-defaults");
    await expect(editor.getByTestId("project-defaults-model-needs-agent")).toBeVisible();
    await expect(editor.getByRole("radio", { name: /Global default/ }).first()).toBeChecked();
    await editor.getByRole("radiogroup", { name: "Agent for new sessions" }).getByRole("radio", { name: /^Codex$/ }).check();
    const model = editor.getByRole("combobox", { name: "Model for new sessions" });
    await model.selectOption("gpt-5-codex");
    await editor.getByRole("radio", { name: "Ask every time" }).check();
    await expect(editor.getByTestId("project-defaults-summary")).toContainText("Codex · gpt-5-codex · ask before acting");
    // An agent that sets its model elsewhere says so instead of offering an empty select.
    await editor.getByRole("radiogroup", { name: "Agent for new sessions" }).getByRole("radio", { name: /^opencode$/ }).check();
    await expect(editor.getByTestId("project-defaults-model-elsewhere")).toBeVisible();
    await editor.getByRole("radiogroup", { name: "Agent for new sessions" }).getByRole("radio", { name: /^Codex$/ }).check();
    await model.selectOption("gpt-5-codex"); // switching agent cleared it: a model belongs to one agent
    if (testInfo.project.name === "mobile") {
      const vw = page.viewportSize()!.width;
      for (const t of await editor.locator("label").all()) {
        const box = await t.boundingBox();
        if (box) expect(box.x + box.width).toBeLessThanOrEqual(vw + 0.5);
      }
    }

    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Choose a playbook" })).toBeVisible();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(page.getByTestId("np-review-launch-defaults")).toHaveText("Codex · gpt-5-codex · ask before acting");
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(page.getByRole("heading", { name: "Project created" })).toBeVisible();
    expect(state.created).toHaveLength(1);
    expect(state.created[0].launch_defaults).toEqual({ engine: "codex", model: "gpt-5-codex", bypass: false });
  });

  test("Settings → Projects saves the block for an existing project", async ({ page }) => {
    const state = await mockApp(page, { projects: [PLAIN] });
    await page.goto(settingsPath("projects"));
    await page.getByRole("button", { name: "Session defaults for Plain" }).click();
    const panel = page.getByRole("region", { name: "Session defaults for Plain" });
    await panel.getByRole("radiogroup", { name: "Agent for new sessions" }).getByRole("radio", { name: /^Codex$/ }).check();
    await panel.getByRole("radio", { name: "Skip prompts" }).check();
    await panel.getByRole("button", { name: /Save session defaults/ }).click();
    await expect.poll(() => state.patches).toEqual([{ launch_defaults: { engine: "codex", bypass: true } }]);
    await expect(page.getByTestId("project-launch-defaults")).toContainText("Codex · skip prompts");
  });
});
