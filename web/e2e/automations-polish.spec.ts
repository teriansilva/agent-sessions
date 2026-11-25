/** Library → Automations polish (#2244), in a real browser on the desktop AND mobile projects.
 *
 *  The operator's phone showed four things, each pinned here:
 *
 *  1. ONE "New automation" on the empty list (the head's), never a second one in the empty state.
 *  2. ONE frame. The app's content pane (`.terminal-pane`) is the chamfered panel (design.md §5:
 *     "the pane *is* the panel"); a page section that draws its own chamfer inside it is a
 *     box-in-a-box. A chamfer is a `clip-path` polygon, so the robust measure is the FRAME DEPTH of
 *     a section: how many of the element and its ancestors are clipped. On a page that renders
 *     right it is 1 (the pane); a nested panel makes it 2. Playbooks is the control. Buttons are
 *     chamfered too, but they are never an ancestor of a heading or a section.
 *  3. The Agent picker is API first: only API agents the server admits, one short line, and a
 *     folded "why" naming each installed API agent that is held, in plain words.
 *  4. Message a session offers API sessions; a stored terminal target stays, stamped. */
import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  AUTOMATION_NEW_PATH,
  AUTOMATIONS_PATH,
  PLAYBOOKS_PATH,
  automationEditPath,
  automationPath,
} from "../src/lib/routes";
import {
  automation,
  mockAutomations,
  targetsAndOverflow,
  toReview,
  toStep,
  wizardHeading,
  type FakeOptions,
} from "./automations";
import { ROSTER, mockRoster } from "./roster";
import { mockWebhooks, webhook } from "./webhooks";

type Json = Record<string, unknown>;

/** The server's `NOT_ADMITTED` (`automation_runner.py`), for a held API client. */
const HELD = "this agent's API client can't run unattended yet";

/** `GET /api/automations` → `engines`: a mix of every kind of row the server sends. */
const ENGINES = [
  { id: "claude", ok: true, reason: "" }, // a terminal agent: never offered for a new automation
  { id: "claude-api", ok: true, reason: "" },
  { id: "opencode-api", ok: true, reason: "" },
  { id: "codex-api", ok: false, reason: HELD },
  { id: "copilot-api", ok: false, reason: HELD },
  { id: "opencode2-api", ok: false, reason: "opencode2: not installed" },
  { id: "opencode2", ok: false, reason: "opencode2: not installed" },
  {
    id: "codex",
    ok: false,
    reason: "codex does not reveal its session id until after its first turn; missing: unattended_launch, seed_start",
  },
  { id: "shell", ok: false, reason: "shell: not an agent engine" },
  { id: "apichat", ok: false, reason: "apichat: not an agent engine" },
];

/** The host: the admitted and held API clients can start; opencode2's API client can't. */
const PRESENT = {
  "claude-api": { present: true, supports_new: true },
  "opencode-api": { present: true, supports_new: true },
  "codex-api": { present: true, supports_new: true },
  "copilot-api": { present: true, supports_new: true },
  "opencode2-api": { present: false },
};

const FAMILIES = [
  {
    family: "claude",
    clients: ["claude", "claude-api"],
    interactive_default: { engine: "claude-api", reason: "api_ready" },
    unattended_admitted: [],
    reasons: {},
  },
];

const RAW = ["unattended_launch", "not installed", "not an agent engine", "session id"];

const label = (id: string) => String(ROSTER.engines.find((e) => e.id === id)!.label);

function session(id: string, engine: string, title: string): Json {
  return {
    id,
    engine,
    uuid: id.split(":")[1],
    short_uuid: id.split(":")[1].slice(0, 8),
    cwd: "/repo",
    project: { kind: "folder", id: "/repo", name: "/repo" },
    last_mtime: 1_700_000_000,
    first_user_message: "",
    title,
    sticky: false,
    archived: false,
    running: true,
  };
}

const API_SESSION = session("claude-api:0b5c3c1e-8a2f-4c55-9d7e-1f2a3b4c5d6e", "claude-api", "API review session");
const TERMINAL_SESSION = session("claude:11111111-2222-3333-4444-555555555555", "claude", "Terminal triage session");
const CHAT_SESSION = session("apichat:22222222-3333-4444-5555-666666666666", "apichat", "Chat thread");

async function setup(page: Page, opts: FakeOptions = {}) {
  const fake = await mockAutomations(page, { engines: ENGINES, ...opts });
  await mockRoster(page, { overrides: PRESENT, families: FAMILIES }); // last: the real roster wins
  await page.route("**/api/fs/dirs**", (r) => r.fulfill({ json: { home: "/repo", path: "/repo", dirs: [] } }));
  return fake;
}

/** How many of `el` and its ancestors draw a chamfer (a `clip-path`). One is the app's pane. */
async function frameDepth(el: Locator): Promise<number> {
  return el.evaluate((node) => {
    let n = 0;
    for (let e: Element | null = node; e; e = e.parentElement)
      if (getComputedStyle(e).clipPath !== "none") n += 1;
    return n;
  });
}

function lastPatch(fake: { requests: { method: string; path: string; body: unknown }[] }) {
  return [...fake.requests].reverse().find((r) => r.method === "PATCH")?.body as Json | undefined;
}

test("the empty list has exactly one New automation, and its explanation", async ({ page }) => {
  await setup(page);
  await page.goto(AUTOMATIONS_PATH);
  const empty = page.getByTestId("automations-empty");
  await expect(empty).toContainText("No automations yet");
  await expect(empty).toContainText("Nothing runs until you enable it");
  const pageRoot = page.getByTestId("automations-page");
  await expect(pageRoot.getByRole("link", { name: /New automation/ })).toHaveCount(1);
  await expect(pageRoot.getByRole("button", { name: /New automation/ })).toHaveCount(0);
  await expect(page.getByTestId("automation-new")).toBeVisible();
});

test("one frame: Automations, its runs and the wizard sit on the pane, as Playbooks does", async ({ page }) => {
  await setup(page, {
    automations: [
      automation(),
      automation({ id: "a2", name: "Paused digest", state: "paused", paused: true, paused_reason: "paused after 3 failed runs in a row", next_run: null }),
    ],
  });
  await mockWebhooks(page, [webhook({ id: "0a1b2c3d4e5f6a7b", name: "shop repo" })]);
  // The control: a Library page that renders right has its heading at depth 1 (the pane).
  await page.goto(PLAYBOOKS_PATH);
  await expect(page.getByRole("heading", { level: 1, name: "Playbooks" })).toBeVisible();
  expect(await frameDepth(page.getByRole("heading", { level: 1, name: "Playbooks" }))).toBe(1);

  await page.goto(AUTOMATIONS_PATH);
  await expect(page.locator("#automations-title")).toBeVisible();
  expect(await frameDepth(page.locator("#automations-title")), "the list's heading").toBe(1);
  expect(await frameDepth(page.getByTestId("why-not-running")), "the why-not-running section").toBe(1);

  await page.goto(automationPath("a1"));
  await expect(page.locator("#runs-title")).toBeVisible();
  expect(await frameDepth(page.locator("#runs-title")), "the runs page's heading").toBe(1);

  await page.goto(AUTOMATION_NEW_PATH);
  await expect(wizardHeading(page)).toBeVisible();
  expect(await frameDepth(wizardHeading(page)), "the wizard's heading").toBe(1);
  await page.getByTestId("trigger-kind").getByRole("button", { name: /When a webhook delivers/ }).click();
  await page.getByTestId("trigger-webhook").selectOption("0a1b2c3d4e5f6a7b");
  await expect(page.getByTestId("shared-webhook-panel")).toBeVisible();
  expect(await frameDepth(page.getByTestId("shared-webhook-panel")), "the webhook section in step 1").toBe(1);

  await page.goto(automationEditPath("a1"));
  await toReview(page);
  await expect(page.getByTestId("editor-scope")).toBeVisible();
  expect(await frameDepth(page.getByTestId("editor-scope")), "the scope section in step 5").toBe(1);
});

test("the Agent picker offers only API agents that can start, and folds away why others are missing", async ({ page }, info) => {
  await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await toStep(page, "What does it do?");
  await page.getByTestId("action-kind").getByRole("button", { name: /Start a session/ }).click();
  const select = page.getByTestId("session-engine");
  await expect(select.locator("option")).toHaveCount(3);
  expect(await select.locator("option").evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value))).toEqual([
    "",
    "claude-api",
    "opencode-api",
  ]);
  await expect(page.getByTestId("engine-api-note")).toHaveText("New automations use API agents.");
  const why = page.getByTestId("engine-why");
  await expect(why).toBeVisible();
  await expect(why).not.toHaveAttribute("open", "");
  await expect(why.locator("summary")).toHaveText("Why aren’t other agents listed?");
  await expect(why.locator("li")).toHaveText([
    `${label("codex-api")}: not cleared for unattended work yet`,
    `${label("copilot-api")}: not cleared for unattended work yet`,
  ]);
  // Hidden or shown, none of the server's raw diagnostics is printed anywhere on the step.
  const text = (await page.getByTestId("automation-editor").textContent()) ?? "";
  for (const raw of RAW) expect(text, `raw reason "${raw}"`).not.toContain(raw);
  await why.locator("summary").click();
  await expect(why).toHaveAttribute("open", "");
  await expect(why.locator("li").first()).toBeVisible();
  await select.selectOption("claude-api");
  await page.getByTestId("session-more").locator("summary").click();
  await targetsAndOverflow(page, '[data-testid="automation-editor"]');
  await page.screenshot({ path: info.outputPath("what-agent.png"), fullPage: true });
});

/** No API agent the server admits: every row refused (the held ones with the held reason). */
const NONE_ELIGIBLE = ENGINES.map((e) => ({ ...e, ok: false, reason: e.reason || HELD }));

test("with no API agent that can start, the step says so in one line, links to Settings → Agents and holds Next", async ({ page }) => {
  await setup(page, { engines: NONE_ELIGIBLE });
  await page.goto(AUTOMATION_NEW_PATH);
  await toStep(page, "What does it do?");
  await page.getByTestId("action-kind").getByRole("button", { name: /Start a session/ }).click();
  await expect(page.getByTestId("session-engine").locator("option")).toHaveCount(1);
  const none = page.getByTestId("engine-none");
  await expect(none).toHaveCount(1);
  await expect(none).toHaveText("No API agent can start a new automation on this server yet. Settings → Agents");
  await expect(none.getByRole("link", { name: "Settings → Agents" })).toHaveAttribute("href", "/settings/agents");
  await expect(page.getByTestId("engine-api-note")).toHaveCount(0);
  // Nothing to pick, so the step can't be left: Next is held and says why.
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await expect(page.getByTestId("step-problems")).toContainText("Choose an agent");
  const text = (await page.getByTestId("automation-editor").textContent()) ?? "";
  for (const raw of RAW) expect(text, `raw reason "${raw}"`).not.toContain(raw);
});

test("a legacy automation on a terminal agent still opens, shows it stamped, and saves", async ({ page }) => {
  const TERMINAL_ACTION = {
    kind: "start_session",
    engine: "claude",
    model: null,
    folder: "/repo",
    bypass: false,
    message: { text: "Triage the inbox" },
  };
  const fake = await setup(page, {
    automations: [automation({ id: "a1", name: "Morning triage", action: TERMINAL_ACTION, enabled: false, state: "off" })],
  });
  await page.goto(automationEditPath("a1"));
  await toStep(page, "What does it do?");
  const select = page.getByTestId("session-engine");
  await expect(select).toHaveValue("claude");
  expect(await select.locator("option").evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value))).toEqual([
    "",
    "claude-api",
    "opencode-api",
    "claude",
  ]);
  await expect(page.getByTestId("automation-legacy-terminal")).toBeVisible();
  await expect(page.getByTestId("automation-switch-api")).toBeEnabled();
  await toReview(page);
  await page.getByTestId("automation-name").fill("Morning triage, renamed");
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
  expect(lastPatch(fake)!.name).toBe("Morning triage, renamed");
  expect((lastPatch(fake)!.action as Json).engine).toBe("claude");
});

test("Message a session offers API sessions; a stored terminal target stays, stamped", async ({ page }) => {
  const SEND = (key: string) => ({ kind: "send_to_session", session_key: key, message: { text: "Status?" } });
  await setup(page, {
    sessions: [API_SESSION, TERMINAL_SESSION, CHAT_SESSION],
    automations: [
      automation({ id: "a2", name: "Poke triage", action: SEND(String(TERMINAL_SESSION.id)), enabled: false, state: "off" }),
    ],
  });
  await page.goto(AUTOMATION_NEW_PATH);
  await toStep(page, "What does it do?");
  await page.getByTestId("action-kind").getByRole("button", { name: /Message a session/ }).click();
  const pick = page.getByTestId("send-session");
  expect(await pick.locator("option").evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value))).toEqual([
    "",
    API_SESSION.id,
  ]);
  await expect(page.getByTestId("send-session-api-note")).toHaveText("New automations message API sessions.");
  await expect(page.getByTestId("automation-legacy-terminal")).toHaveCount(0);

  await page.goto(automationEditPath("a2"));
  await toStep(page, "What does it do?");
  await expect(pick).toHaveValue(String(TERMINAL_SESSION.id));
  expect(await pick.locator("option").evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value))).toEqual([
    "",
    API_SESSION.id,
    TERMINAL_SESSION.id,
  ]);
  await expect(page.getByTestId("automation-legacy-terminal")).toBeVisible();
});

test("a legacy Message-a-session automation whose terminal session isn't listed keeps it, and saves it", async ({ page }) => {
  const GONE = "claude:99999999-8888-7777-6666-555555555555"; // a terminal session the list no longer has
  const fake = await setup(page, {
    sessions: [API_SESSION, TERMINAL_SESSION],
    automations: [
      automation({
        id: "a3",
        name: "Poke the old session",
        action: { kind: "send_to_session", session_key: GONE, message: { text: "Status?" } },
        enabled: false,
        state: "off",
      }),
    ],
  });
  await page.goto(automationEditPath("a3"));
  await toStep(page, "What does it do?");
  const pick = page.getByTestId("send-session");
  await expect(pick).toHaveValue(GONE);
  // Its own target is kept; no other terminal session is offered.
  expect(await pick.locator("option").evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value))).toEqual([
    "",
    API_SESSION.id,
    GONE,
  ]);
  await expect(page.getByTestId("send-session-key")).toHaveText(GONE);
  await expect(page.getByTestId("automation-legacy-terminal")).toBeVisible();
  await toReview(page);
  await page.getByTestId("automation-name").fill("Poke the old session, renamed");
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
  const sent = lastPatch(fake)!;
  expect(sent.name).toBe("Poke the old session, renamed");
  expect((sent.action as Json).session_key).toBe(GONE);
});

test("captures the empty list and the What step", async ({ page }, info) => {
  await setup(page);
  await page.goto(AUTOMATIONS_PATH);
  await expect(page.getByTestId("automations-empty")).toBeVisible();
  await targetsAndOverflow(page, '[data-testid="automations-page"]');
  await page.screenshot({ path: info.outputPath("automations-empty.png"), fullPage: true });
  await page.goto(AUTOMATION_NEW_PATH);
  await toStep(page, "What does it do?");
  await page.getByTestId("action-kind").getByRole("button", { name: /Start a session/ }).click();
  await page.getByTestId("engine-why").locator("summary").click();
  await targetsAndOverflow(page, '[data-testid="automation-editor"]');
  await page.screenshot({ path: info.outputPath("what-step.png"), fullPage: true });
});

for (const theme of ["dark", "light"] as const) {
  test(`captures the What step with the why fold open — ${theme}`, async ({ page }, info) => {
    // The theme is the app's own choice (`tr-theme`); the init script runs before the app reads it.
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await setup(page);
    await page.goto(AUTOMATION_NEW_PATH);
    await toStep(page, "What does it do?");
    await page.getByTestId("action-kind").getByRole("button", { name: /Start a session/ }).click();
    const why = page.getByTestId("engine-why");
    await why.locator("summary").click();
    await expect(why).toHaveAttribute("open", "");
    await why.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`what-why-open-${theme}.png`) });
  });

  test(`captures the What step with no eligible API agent — ${theme}`, async ({ page }, info) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await setup(page, { engines: NONE_ELIGIBLE });
    await page.goto(AUTOMATION_NEW_PATH);
    await toStep(page, "What does it do?");
    await page.getByTestId("action-kind").getByRole("button", { name: /Start a session/ }).click();
    await expect(page.getByTestId("engine-none")).toBeVisible();
    await page.getByTestId("engine-none").scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`what-no-agent-${theme}.png`) });
  });
}
