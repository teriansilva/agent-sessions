/** API first for automations (#1728 Phase 2), in a real browser on desktop and phone, both themes.
 *
 *  A start_session automation on a console client is labelled "Terminal — unreliable unattended"
 *  (a neutral stamp: it keeps its own state LED and word) and offers "Switch to API client" (44 px)
 *  when its family has an API client. Switch opens a sheet naming the new client, model and
 *  permission mode; Confirm saves through the ordinary PATCH with the API client, its default model
 *  and "asks before each request" — the terminal's permission grant is not carried — and an ENABLED
 *  automation then asks for the server's consent. No cutoff: a new console-target automation still
 *  saves. With no API client admitted, the button stays disabled with the server's reason. */
import { expect, test, type Page } from "@playwright/test";

import { AUTOMATIONS_PATH, automationEditPath } from "../src/lib/routes";
import { automation, mockAutomations, type FakeOptions } from "./automations";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

const FAMILIES = [
  {
    family: "claude",
    clients: ["claude", "claude-api"],
    interactive_default: { engine: "claude-api", reason: "api_ready" },
    unattended_admitted: [],
    reasons: {},
  },
  {
    family: "gemini",
    clients: ["gemini"],
    interactive_default: { engine: "gemini", reason: "api_absent" },
    unattended_admitted: [],
    reasons: {},
  },
];

const TERMINAL_ACTION = {
  kind: "start_session",
  engine: "claude",
  model: null,
  folder: "/repo",
  bypass: true,
  message: { text: "Triage the inbox" },
};

/** Admitted = what #1273 P3 will provide; today the server reports the API client not ok. */
const ADMITTED = [
  { id: "claude", ok: true, reason: "" },
  { id: "claude-api", ok: true, reason: "" },
  { id: "gemini", ok: true, reason: "" },
];
const NOT_ADMITTED = [
  { id: "claude", ok: true, reason: "" },
  { id: "claude-api", ok: false, reason: "claude-api cannot be started with a first message" },
];

async function setup(page: Page, opts: FakeOptions) {
  const fake = await mockAutomations(page, opts);
  await mockRoster(page, { families: FAMILIES }); // registered last: wins over the common mock
  return fake;
}

function lastPatch(fake: { requests: { method: string; path: string; body: unknown }[] }) {
  return [...fake.requests].reverse().find((r) => r.method === "PATCH");
}

test("a terminal automation is labelled and switches to its API client after a fresh confirmation (#1728)", async ({ page }) => {
  const fake = await setup(page, {
    engines: ADMITTED,
    automations: [
      automation({ id: "a1", name: "Morning triage", action: TERMINAL_ACTION, enabled: false, state: "off" }),
      automation({
        id: "a2",
        name: "Gemini digest",
        action: { ...TERMINAL_ACTION, engine: "gemini" },
      }),
      automation({ id: "a3", name: "Nightly audit" }), // a mission: no label
    ],
  });
  await page.goto(AUTOMATIONS_PATH);
  const row = page.locator('[data-testid="automation-row"][data-id="a1"]');
  await expect(row.getByTestId("automation-legacy-terminal")).toHaveText(/Terminal — unreliable unattended/i);
  // The label is a stamp beside the state, which keeps its own word.
  await expect(row).toContainText("Off");
  // A console target with no API client in its family: labelled, nothing to switch to.
  const lone = page.locator('[data-testid="automation-row"][data-id="a2"]');
  await expect(lone.getByTestId("automation-legacy-terminal")).toBeVisible();
  await expect(lone.getByTestId("automation-switch-api")).toHaveCount(0);
  await expect(
    page.locator('[data-testid="automation-row"][data-id="a3"]').getByTestId("automation-legacy-terminal"),
  ).toHaveCount(0);

  const sw = row.getByTestId("automation-switch-api");
  expect((await sw.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await sw.click();
  const sheet = page.getByTestId("automation-switch-sheet");
  await expect(sheet).toBeVisible();
  const facts = sheet.getByTestId("automation-switch-facts");
  await expect(facts).toContainText("Claude Code");
  await expect(facts).toContainText("Claude — API");
  await expect(facts).toContainText("the agent’s default");
  await expect(facts).toContainText("asks before each request");
  expect(fake.requests.filter((r) => r.method === "PATCH")).toHaveLength(0); // nothing saved yet
  await sheet.getByTestId("automation-switch-confirm").click();
  await expect(sheet).toBeHidden();
  const sent = lastPatch(fake)!.body as { action: Record<string, unknown> };
  expect(sent.action).toEqual({ ...TERMINAL_ACTION, engine: "claude-api", model: null, bypass: false });
  await expect(page.getByTestId("automations-notice")).toContainText("now runs on Claude — API");
  await expect(row.getByTestId("automation-legacy-terminal")).toHaveCount(0);
});

test("switching an ENABLED automation asks for the server's consent before it saves (#1728)", async ({ page }) => {
  const fake = await setup(page, {
    engines: ADMITTED,
    widenOnPatch: ["a different agent or model"],
    automations: [automation({ id: "a1", name: "Morning triage", action: TERMINAL_ACTION })],
  });
  await page.goto(AUTOMATIONS_PATH);
  const row = page.locator('[data-testid="automation-row"][data-id="a1"]');
  await row.getByTestId("automation-switch-api").click();
  await page.getByTestId("automation-switch-confirm").click();
  const consent = page.getByTestId("consent-dialog");
  await expect(consent).toBeVisible();
  await expect(consent).toContainText("a different agent or model");
  await expect(consent.getByTestId("consent-confirm")).toBeDisabled(); // consent is an act
  await consent.getByTestId("consent-agree").check();
  await consent.getByTestId("consent-confirm").click();
  await expect(consent).toBeHidden();
  const sent = lastPatch(fake)!.body as Record<string, unknown>;
  expect(sent).toMatchObject({ consent: true, scope_digest: "digest-a1-2" });
  expect((sent.action as Record<string, unknown>).bypass).toBe(false);
  expect((sent.action as Record<string, unknown>).engine).toBe("claude-api");
});

test("no API client admitted: Switch is disabled and says why — never a terminal fallback (#1728)", async ({ page }) => {
  await setup(page, {
    engines: NOT_ADMITTED,
    automations: [automation({ id: "a1", name: "Morning triage", action: TERMINAL_ACTION })],
  });
  await page.goto(AUTOMATIONS_PATH);
  const row = page.locator('[data-testid="automation-row"][data-id="a1"]');
  await expect(row.getByTestId("automation-switch-api")).toBeDisabled();
  await expect(row.getByTestId("automation-switch-why")).toContainText("cannot be started with a first message");
});

test("the editor labels a terminal target, keeps saving it, and never switches an unsaved draft (#1728)", async ({ page }) => {
  const fake = await setup(page, {
    engines: ADMITTED,
    automations: [
      automation({ id: "a1", name: "Morning triage", action: TERMINAL_ACTION, enabled: false, state: "off" }),
    ],
  });
  await page.goto(automationEditPath("a1"));
  const engine = page.getByTestId("session-engine");
  await expect(engine).toHaveValue("claude");
  await expect(page.getByTestId("automation-legacy-terminal")).toBeVisible();
  // The label follows what the form targets: an API client is not a terminal target.
  await engine.selectOption("claude-api");
  await expect(page.getByTestId("automation-legacy-terminal")).toHaveCount(0);
  await engine.selectOption("claude");
  await expect(page.getByTestId("automation-legacy-terminal")).toBeVisible();
  // An unsaved draft is never carried along by a switch.
  await page.getByTestId("automation-name").fill("Morning triage, earlier");
  await expect(page.getByTestId("automation-switch-api")).toBeDisabled();
  await expect(page.getByTestId("automation-switch-why")).toContainText("Save or discard your changes first");
  // No cutoff in Phase 2: a console-target save goes through as it always did.
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
  const sent = lastPatch(fake)!.body as { action: Record<string, unknown> };
  expect(sent.action.engine).toBe("claude");
  await expect(page.getByTestId("automation-switch-api")).toBeEnabled();
});

for (const theme of ["dark", "light"] as const) {
  test(`captures the label and the switch sheet — ${theme} (#1728)`, async ({ page }, info) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await setup(page, {
      engines: ADMITTED,
      automations: [automation({ id: "a1", name: "Morning triage", action: TERMINAL_ACTION })],
    });
    await page.goto(automationEditPath("a1"));
    await expect(page.getByTestId("automation-legacy-terminal")).toBeVisible();
    await expect(page.getByTestId("automation-switch-api")).toBeEnabled();
    await page.screenshot({ path: info.outputPath(`automation-legacy-${theme}.png`), fullPage: true });
    await page.getByTestId("automation-switch-api").click();
    await expect(page.getByTestId("automation-switch-sheet")).toBeVisible();
    await page.screenshot({ path: info.outputPath(`automation-switch-${theme}.png`) });
  });
}
