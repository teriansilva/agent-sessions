import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";
import { agentPicker, launchFolder, projectPicker } from "./newSessionForm";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";

// #1436: the polished New session form, in a real browser on the desktop AND mobile projects —
// what jsdom cannot show: the tiles really are ≥44px and clickable, the Start footer is on
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
  opts: { projectsDelayMs?: number; projects?: typeof PROJECTS; defaultProject?: string } = {},
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
        ...(opts.defaultProject ? { default_project: opts.defaultProject } : {}),
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

test("pick an agent tile and a project, start — the project's folder, no folder card (#1436, #1458)", async ({ page }) => {
  const calls = await mockBackend(page);
  await page.goto(NEW_SESSION_PATH);
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

  // #1458: the form defines no folder — no folder line, no picker, no per-session override. The
  // folder is the project's, and the start summary names it (its full path is data-cwd).
  await expect(page.getByTestId("launch-folder")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /choose folder/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /use project default/i })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Folder" })).toHaveCount(0);
  await expect(project.radio("p-1")).toBeChecked(); // the starred project
  await expect(launchFolder(page)).toHaveAttribute("data-cwd", "/home/u/claude/agent-sessions");
  await expect(page.getByTestId("new-session-summary")).toContainText(
    "Start Codex in ~/claude/agent-sessions",
  );

  await project.selectOption("p-2");
  await expect(launchFolder(page)).toHaveAttribute("data-cwd", "/home/u/claude/payments-api");
  await expect(page.getByTestId("new-session-summary")).toContainText("Start Codex in ~/claude/payments-api");

  // The launch picker asked for the COUNT-FREE list.
  expect(calls).toContain("?counts=0");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(/\/s\/codex\/new-/);
});

test("every tile, row and the Start button are ≥44px tall (#1436, design §8)", async ({ page }) => {
  await mockBackend(page);
  await page.goto(NEW_SESSION_PATH);
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  const boxes = await page
    .locator(
      'label:has(> input[name="new-session-agent"]), label:has(> input[name="new-session-project"])',
    )
    .evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().height)));
  expect(boxes.length).toBeGreaterThan(6);
  for (const h of boxes) expect(h).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  const start = await page.getByRole("button", { name: /start session/i }).boundingBox();
  expect(start!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
});

test("Start is on screen without scrolling on a phone (#1436)", async ({ page }) => {
  await mockBackend(page);
  await page.goto(NEW_SESSION_PATH);
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  const start = page.getByRole("button", { name: /start session/i });
  await expect(start).toBeInViewport();
  const vh = page.viewportSize()!.height;
  const box = await start.boundingBox();
  expect(box!.y + box!.height).toBeLessThanOrEqual(vh);
});

test("arrow keys move the agent choice; Ctrl+Enter starts (#1436)", async ({ page }) => {
  await mockBackend(page);
  await page.goto(NEW_SESSION_PATH);
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
  await page.goto(NEW_SESSION_PATH);
  // First-ever visit: fixed-height skeleton rows, then the list.
  await expect(page.getByTestId("project-skeleton").first()).toBeVisible();
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  // The first element BELOW the fixed-height list — it must not move when the list lands (#1458:
  // the folder card that used to anchor this is gone; the checkbox is the first flow element after).
  const belowList = page.getByRole("checkbox", { name: /skip permission/i });
  const belowY = (await belowList.boundingBox())!.y;

  // Leave and come back inside the SPA: the module cache outlives the route.
  await page.evaluate(() => {
    history.pushState({}, "", "/settings");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.getByRole("radiogroup", { name: "Project" })).toHaveCount(0);
  await page.evaluate((p) => {
    history.pushState({}, "", p);
    dispatchEvent(new PopStateEvent("popstate"));
  }, NEW_SESSION_PATH);
  // Rows are there before the (slow) revalidation lands; Start waits for it.
  await expect(projectPicker(page).radio("p-1")).toBeChecked({ timeout: 500 });
  await expect(page.getByTestId("project-skeleton")).toHaveCount(0);
  expect((await belowList.boundingBox())!.y).toBe(belowY);
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

// Hermes on #1437, finding 3: the FIRST visit must not jump either — at any list size. What sits
// below the fixed-height list holds its y under the skeleton and under the loaded list, for two
// projects and ten.
for (const [label, projects] of [
  ["two projects", PROJECTS],
  ["ten projects", MANY],
] as const) {
  test(`a first visit does not shift the form when the list lands — ${label} (#1437)`, async ({ page }) => {
    await mockBackend(page, { projectsDelayMs: 1500, projects: [...projects] });
    await page.goto(NEW_SESSION_PATH);
    await expect(page.getByTestId("project-skeleton").first()).toBeVisible();
    const before = (await page.getByRole("checkbox", { name: /skip permission/i }).boundingBox())!.y;
    await expect(page.getByTestId("project-skeleton")).toHaveCount(0);
    await expect(page.getByRole("radiogroup", { name: "Project" }).getByRole("radio").first()).toBeAttached();
    expect((await page.getByRole("checkbox", { name: /skip permission/i }).boundingBox())!.y).toBe(before);
    if (projects.length > 6) {
      // The filter lives INSIDE the fixed box, and the long list scrolls within it.
      await expect(page.getByRole("searchbox", { name: "Filter projects" })).toBeVisible();
    }
  });
}

test("a wide pane puts agent and project side by side; a narrow one stacks them (#1473)", async ({ page }, info) => {
  await mockBackend(page);
  if (info.project.name === "desktop") await page.setViewportSize({ width: 1700, height: 1000 });
  await page.goto(NEW_SESSION_PATH);
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  const agentBox = (await page.getByRole("radiogroup", { name: "Agent" }).boundingBox())!;
  const projectBox = (await page.getByRole("radiogroup", { name: "Project" }).boundingBox())!;
  if (info.project.name === "desktop") {
    expect(projectBox.x).toBeGreaterThan(agentBox.x + agentBox.width - 1); // right of it
    expect(Math.abs(projectBox.y - agentBox.y)).toBeLessThan(120); // same band, not below
  } else {
    expect(projectBox.y).toBeGreaterThan(agentBox.y + agentBox.height - 1); // stacked
  }
  await expect(page.getByRole("button", { name: /start session/i })).toBeInViewport();
});

test("an API agent starts at the stored skip-permissions default (#1473)", async ({ page }) => {
  await mockBackend(page); // agent_defaults.bypass = true
  await page.goto(NEW_SESSION_PATH);
  await agentPicker(page).selectOption("codex-api");
  const skip = page.getByRole("checkbox", { name: /skip permission prompts/i });
  await expect(skip).toBeChecked();
  await expect(page.getByTestId("api-bypass-warning")).toBeVisible();
  await expect(page.getByTestId("new-session-summary")).toContainText("skips permission prompts");
});

test("a project with no folder and no global default points at Settings → Projects, and Start waits (#1458)", async ({ page }) => {
  await mockBackend(page, {
    projects: [{ ...PROJECTS[0], folders: [], default_folder: "" }],
  });
  await page.goto(NEW_SESSION_PATH);
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  const summary = page.getByTestId("new-session-summary");
  await expect(summary).toContainText(
    "“agent-sessions” has no default folder — set one in Settings → Projects.",
  );
  await expect(summary).toHaveAttribute("data-cwd", "");
  await expect(page.getByRole("button", { name: /start session/i })).toBeDisabled();
  // A real link, a 44px target (design §8), reachable from the keyboard — and it goes there.
  const link = summary.getByRole("link", { name: "Settings → Projects" });
  expect((await link.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await link.focus();
  await expect(link).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/settings\/projects$/);
});

test("a folderless project with a global default starts there, and the summary says so (#1458)", async ({ page }) => {
  await mockBackend(page, {
    projects: [{ ...PROJECTS[0], folders: [], default_folder: "" }],
    defaultProject: "/home/u/legacy",
  });
  await page.goto(NEW_SESSION_PATH);
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  const summary = page.getByTestId("new-session-summary");
  await expect(summary).toHaveAttribute("data-cwd", "/home/u/legacy");
  await expect(summary).toContainText("Start Claude Code in ~/legacy");
  await expect(summary).toContainText("project agent-sessions · using your global default folder");
  await expect(page.getByRole("button", { name: /start session/i })).toBeEnabled();
});

test("no project and no global default: “Pick a project to start in.”, Start disabled (#1458)", async ({ page }) => {
  await mockBackend(page);
  await page.goto(NEW_SESSION_PATH);
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  await projectPicker(page).selectOption("");
  await expect(page.getByTestId("new-session-summary")).toHaveText("Pick a project to start in.");
  await expect(page.getByRole("button", { name: /start session/i })).toBeDisabled();
});

// Captures for review (#1436): both themes on both projects, the console default and an API
// client. Written to the test output dir — no pixel assertion, the checks above are the gate.
for (const theme of ["dark", "light"] as const) {
  test(`captures the form — ${theme} (#1436)`, async ({ page }, info) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await mockBackend(page);
    await page.goto(NEW_SESSION_PATH);
    await expect(projectPicker(page).radio("p-1")).toBeChecked();
    await expect(page.getByRole("button", { name: /start session/i })).toBeEnabled();
    await page.screenshot({ path: info.outputPath(`new-session-${theme}.png`) });
    await agentPicker(page).selectOption("codex-api");
    await page.screenshot({ path: info.outputPath(`new-session-${theme}-api.png`), fullPage: true });
    if (info.project.name === "mobile") {
      // The lower half on a phone (Hermes on #1473): the skip control, in view. Scroll the form
      // above the fixed footer to reveal the remaining controls.
      await page.getByRole("checkbox", { name: /skip permission/i }).evaluate((el) => {
        let p: HTMLElement | null = el.parentElement;
        while (p && getComputedStyle(p).overflowY !== "auto") p = p.parentElement;
        p?.scrollTo(0, p.scrollHeight);
      });
      await page.screenshot({ path: info.outputPath(`new-session-${theme}-lower.png`) });
    }
    if (info.project.name === "desktop") {
      await page.setViewportSize({ width: 1700, height: 1000 });
      await page.screenshot({ path: info.outputPath(`new-session-${theme}-wide.png`) });
    }
  });
}

// #1478: the footer fills the pane, including the spare space below a short form.
test("Start footer stays flush with the pane while the form scrolls", async ({ page }, info) => {
  await mockBackend(page);
  if (info.project.name === "desktop") await page.setViewportSize({ width: 1700, height: 1100 });
  await page.goto(NEW_SESSION_PATH);
  const start = page.getByRole("button", { name: "Start session", exact: true });
  await expect(start).toBeEnabled();
  const footer = page.getByTestId("new-session-summary").locator("..");
  const pane = page.locator(".terminal-pane");
  const flush = async () => {
    const f = (await footer.boundingBox())!, p = (await pane.boundingBox())!;
    expect(Math.abs(f.x - p.x)).toBeLessThanOrEqual(2);
    expect(Math.abs(f.width - p.width)).toBeLessThanOrEqual(4);
    expect(Math.abs(f.y + f.height - p.y - p.height)).toBeLessThanOrEqual(2);
    await expect(start).toBeInViewport();
  };
  await flush();
  await page.setViewportSize({ width: info.project.name === "desktop" ? 1280 : 320, height: 480 });
  await flush();
  await page.getByRole("radiogroup", { name: "Project" }).scrollIntoViewIfNeeded();
  await flush();
  const skip = page.getByRole("checkbox", { name: /skip permission prompts/i });
  await skip.scrollIntoViewIfNeeded();
  const s = (await skip.boundingBox())!, f = (await footer.boundingBox())!;
  expect(s.y + s.height).toBeLessThanOrEqual(f.y);
  // The phone's home-indicator inset belongs INSIDE the footer, never an outer gap.
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setSafeAreaInsetsOverride", { insets: { bottom: 24 } });
  await expect(footer).toHaveCSS("padding-bottom", "40px");
  await flush();
  await skip.scrollIntoViewIfNeeded();
  expect((await skip.boundingBox())!.y + (await skip.boundingBox())!.height)
    .toBeLessThanOrEqual((await footer.boundingBox())!.y);
  await page.screenshot({ path: info.outputPath("footer-short.png") });
});
