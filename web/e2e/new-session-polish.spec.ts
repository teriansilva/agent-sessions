import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";
import { agentPicker, launchFolder, projectPicker } from "./newSessionForm";

// #1436: the polished New session form, in a real browser on the desktop AND mobile projects —
// what jsdom cannot show: the tiles really are ≥44px and clickable, the sticky Start bar is on
// screen without scrolling on a phone, the cached project list paints with no layout shift on a
// second visit, and the keyboard (arrow keys, Ctrl+Enter) drives the native radios.

const PROJECTS = [
  {
    id: "p-1",
    name: "agent-sessions",
    color: "#ffb000",
    folders: ["/home/u/claude/agent-sessions", "/home/u/claude/agent-sessions/web"],
    default_folder: "/home/u/claude/agent-sessions",
    archived: false,
    created_at: 0,
  },
  {
    id: "p-2",
    name: "payments-api",
    color: "#3b9eff",
    folders: ["/home/u/claude/payments-api"],
    default_folder: "/home/u/claude/payments-api",
    archived: false,
    created_at: 0,
  },
];

async function mockBackend(
  page: Page,
  opts: { projectsDelayMs?: number; projects?: typeof PROJECTS } = {},
) {
  const projectCalls: string[] = [];
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude", "codex", "opencode", "codex-api"],
        unavailable_clients: [
          { id: "claude-api", label: "Claude — API", reason: "claude 2.1.287 or later is required" },
        ],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project_id: "p-1",
        agent_defaults: { default_engine: "claude", bypass: true },
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, async (r) => {
    projectCalls.push(new URL(r.request().url()).search);
    if (opts.projectsDelayMs) await new Promise((res) => setTimeout(res, opts.projectsDelayMs));
    await r.fulfill({ json: { projects: opts.projects ?? PROJECTS } });
  });
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/structured/**", (r) =>
    r.fulfill({ json: { status: "ok", reason: null, models: [] } }),
  );
  await mockRoster(page);
  return projectCalls;
}

test("pick an agent tile and a project, override and reset the folder, start (#1436)", async ({ page }) => {
  const calls = await mockBackend(page);
  await page.goto("/");
  const agent = agentPicker(page);
  const project = projectPicker(page);
  await expect(agent.radio("claude")).toBeChecked(); // the stored default…
  await expect(page.locator('label:has(> input[value="claude"])')).toContainText("default");
  // The old boxes are gone; the unavailable client is a disabled tile with its reason.
  await expect(page.getByTestId("new-session-api-about")).toHaveCount(0);
  await expect(agent.radio("unavailable:claude-api")).toBeDisabled();
  await expect(page.getByTestId("new-session-unavailable")).toContainText("2.1.287 or later");

  await agent.selectOption("codex");
  await expect(agent.radio("codex")).toBeChecked();

  await expect(project.radio("p-1")).toBeChecked(); // the starred project
  await expect(launchFolder(page)).toHaveAttribute("data-path", "/home/u/claude/agent-sessions");
  await expect(page.getByText(/default folder for “agent-sessions”/i)).toBeVisible();
  await page.getByRole("group", { name: "Project folders" }).getByRole("button", { name: "~/claude/agent-sessions/web" }).click();
  await expect(launchFolder(page)).toHaveAttribute("data-path", "/home/u/claude/agent-sessions/web");
  await expect(page.getByText("Just for this session")).toBeVisible();
  await page.getByRole("button", { name: /use project default/i }).click();
  await expect(launchFolder(page)).toHaveAttribute("data-path", "/home/u/claude/agent-sessions");

  await project.selectOption("p-2");
  await expect(launchFolder(page)).toHaveAttribute("data-path", "/home/u/claude/payments-api");
  await expect(page.getByTestId("new-session-summary")).toContainText("Start Codex in ~/claude/payments-api");

  // The launch picker asked for the COUNT-FREE list.
  expect(calls).toContain("?counts=0");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(/\/s\/codex\/new-/);
});

test("every tile, row and the Start button are ≥44px tall (#1436, design §8)", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  const boxes = await page
    .locator(
      'label:has(> input[name="new-session-agent"]), label:has(> input[name="new-session-project"]), [data-testid="launch-folder"] button',
    )
    .evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().height)));
  expect(boxes.length).toBeGreaterThan(6);
  for (const h of boxes) expect(h).toBeGreaterThanOrEqual(44);
  const start = await page.getByRole("button", { name: /start session/i }).boundingBox();
  expect(start!.height).toBeGreaterThanOrEqual(44);
});

test("Start is on screen without scrolling — sticky on a phone (#1436)", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  const start = page.getByRole("button", { name: /start session/i });
  await expect(start).toBeInViewport();
  const vh = page.viewportSize()!.height;
  const box = await start.boundingBox();
  expect(box!.y + box!.height).toBeLessThanOrEqual(vh);
});

test("arrow keys move the agent choice; Ctrl+Enter starts (#1436)", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  const agent = agentPicker(page);
  await expect(agent.radio("claude")).toBeChecked();
  await agent.radio("claude").focus();
  await page.keyboard.press("ArrowRight");
  await expect(agent.radio("codex")).toBeChecked();
  await page.keyboard.press("Control+Enter");
  await expect(page).toHaveURL(/\/s\/codex\/new-/);
});

test("a second visit paints the cached projects at once — no layout shift (#1436)", async ({ page }) => {
  await mockBackend(page, { projectsDelayMs: 1500 });
  await page.goto("/");
  // First-ever visit: fixed-height skeleton rows, then the list.
  await expect(page.getByTestId("project-skeleton").first()).toBeVisible();
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  const folderY = (await launchFolder(page).boundingBox())!.y;

  // Leave and come back inside the SPA: the module cache outlives the route.
  await page.evaluate(() => {
    history.pushState({}, "", "/settings");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.getByRole("radiogroup", { name: "Project" })).toHaveCount(0);
  await page.evaluate(() => {
    history.pushState({}, "", "/");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  // Rows are there before the (slow) revalidation lands; Start waits for it.
  await expect(projectPicker(page).radio("p-1")).toBeChecked({ timeout: 500 });
  await expect(page.getByTestId("project-skeleton")).toHaveCount(0);
  expect((await launchFolder(page).boundingBox())!.y).toBe(folderY);
  await expect(page.getByRole("button", { name: /start session/i })).toBeDisabled();
  await expect(page.getByRole("button", { name: /start session/i })).toBeEnabled();
});

const MANY = Array.from({ length: 10 }, (_, i) => ({
  ...PROJECTS[1],
  id: `p-m${i}`,
  name: `project-${String(i).padStart(2, "0")}`,
  folders: [`/home/u/code/project-${i}`],
  default_folder: `/home/u/code/project-${i}`,
}));

// Hermes on #1437, finding 3: the FIRST visit must not jump either — at any list size. The folder
// card sits at the same y under the skeleton and under the loaded list, for two projects and ten.
for (const [label, projects] of [
  ["two projects", PROJECTS],
  ["ten projects", MANY],
] as const) {
  test(`a first visit does not shift the form when the list lands — ${label} (#1437)`, async ({ page }) => {
    await mockBackend(page, { projectsDelayMs: 1500, projects: [...projects] });
    await page.goto("/");
    await expect(page.getByTestId("project-skeleton").first()).toBeVisible();
    const before = (await launchFolder(page).boundingBox())!.y;
    await expect(page.getByTestId("project-skeleton")).toHaveCount(0);
    await expect(page.getByRole("radiogroup", { name: "Project" }).getByRole("radio").first()).toBeAttached();
    expect((await launchFolder(page).boundingBox())!.y).toBe(before);
    if (projects.length > 6) {
      // The filter lives INSIDE the fixed box, and the long list scrolls within it.
      await expect(page.getByRole("searchbox", { name: "Filter projects" })).toBeVisible();
    }
  });
}

test("Ctrl+Enter while the folder picker is still loading does not start a session (Hermes 6231)", async ({ page }) => {
  await mockBackend(page);
  // Hold the picker's directory listing: until it answers, focus stays on "Choose folder…".
  let release!: () => void;
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/fs/dirs**", async (r) => {
    await held;
    await r.fulfill({ json: { path: "/home/u", home: "/home/u", dirs: [] } });
  });
  await page.goto("/");
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  await page.getByRole("button", { name: /choose folder/i }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("button", { name: /choose folder/i })).toBeFocused();
  await page.keyboard.press("Control+Enter");
  await page.waitForTimeout(300); // a launch would navigate at once; give it the chance to
  await expect(page).toHaveURL(/\/$/);
  release();
});

// Captures for review (#1436): both themes on both projects, the console default and an API
// client. Written to the test output dir — no pixel assertion, the checks above are the gate.
for (const theme of ["dark", "light"] as const) {
  test(`captures the form — ${theme} (#1436)`, async ({ page }, info) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await mockBackend(page);
    await page.goto("/");
    await expect(projectPicker(page).radio("p-1")).toBeChecked();
    await expect(page.getByRole("button", { name: /start session/i })).toBeEnabled();
    await page.screenshot({ path: info.outputPath(`new-session-${theme}.png`) });
    await agentPicker(page).selectOption("codex-api");
    await page.getByRole("group", { name: "Project folders" }).getByRole("button", { name: "~/claude/agent-sessions/web" }).click();
    await page.screenshot({ path: info.outputPath(`new-session-${theme}-api.png`), fullPage: true });
  });
}
