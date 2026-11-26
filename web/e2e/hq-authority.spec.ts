/** Settings → AI → HQ → Authority (#2390, epic #2070 A1), in a real browser on desktop and mobile:
 *  the presets and per-class Ask / Act controls save exactly what was chosen against the read
 *  revision; a stale revision says so and offers Reload; RISKY rows are stamped and a class HQ
 *  can't act on yet says its choice is kept; every control is a 44 px target on a phone and the
 *  page never scrolls sideways. And a card HQ ran itself reads "Done by HQ", with no buttons.
 *  A1b (#2391): Starts or sends acts; its "also after untrusted text" switch is labelled off and
 *  RISKY · ON, a 44 px target, reachable and toggled by the keyboard, CAS-saved (409 → Reload);
 *  the conversation shows Started by HQ (with a working Open), Sent by HQ, Ran by HQ, and a card
 *  that asks first saying why.
 *  Screenshots in both themes go to the test's output dir (the issue and PR attach them). */
import { expect, test, type Page } from "@playwright/test";

import { mockShell } from "./callFixture";
import { open as openCard, PID, shell } from "./askOperationShell";
import { openAsk, pickConversation } from "./askSidebar";
import { TOUCH_FLOOR } from "./touchTarget";

const PHONE = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;
/** The page scrolls inside its own panel: a tall window lets one shot hold the whole section. */
async function tall(page: Page) {
  const vp = page.viewportSize()!;
  if (vp.height < 2200) await page.setViewportSize({ width: vp.width, height: 2200 });
}
type Classes = Record<string, "ask" | "act">;
const ALL = ["reversible", "starts", "destructive", "external", "widens"];

function derive(classes: Classes): string {
  const acts = ALL.filter((c) => classes[c] === "act");
  if (acts.length === 0) return "ask_first";
  if (acts.length === ALL.length) return "full";
  if (acts.length === 1 && acts[0] === "reversible") return "standard";
  return "custom";
}

async function authority(
  page: Page,
  start: Classes,
  opts: { conflict?: boolean; acts?: unknown[]; untrusted?: boolean } = {},
) {
  const saves: Record<string, unknown>[] = [];
  let classes = { ...start };
  let untrusted = { starts: opts.untrusted ?? false };
  let revision = 4;
  const view = () => ({
    classes,
    preset: derive(classes),
    revision,
    actable: ["reversible", "starts"],
    risky: ["destructive", "external", "widens"],
    untrusted,
    untrusted_classes: ["starts"],
  });
  await mockShell(page);
  await page.route("**/api/hq/authority", async (r) => {
    if (r.request().method() === "GET") return r.fulfill({ json: view() });
    const body = r.request().postDataJSON() as Record<string, unknown>;
    saves.push(body);
    if (opts.conflict || body.revision !== revision)
      return r.fulfill({ status: 409, json: { detail: "authority changed; reload" } });
    if (body.untrusted) {
      untrusted = { ...untrusted, ...(body.untrusted as { starts: boolean }) };
    } else if (typeof body.preset === "string") {
      const act = (c: string) =>
        body.preset === "full" || (body.preset === "standard" && c === "reversible");
      classes = Object.fromEntries(ALL.map((c) => [c, act(c) ? "act" : "ask"])) as Classes;
    } else classes = { ...classes, ...(body.classes as Classes) };
    revision += 1;
    return r.fulfill({ json: view() });
  });
  await page.route("**/api/hq/acts", (r) => r.fulfill({ json: { acts: opts.acts ?? [] } }));
  return saves;
}

const ASK: Classes = Object.fromEntries(ALL.map((c) => [c, "ask"])) as Classes;
const ACTS = [
  { ts: 1764102000, op: "session.rename", label: "Rename", title: "docs-site: rewrite the install guide", state: "done", outcome: "Renamed.", via: "typed", conversation: "c" },
  { ts: 1764101400, op: "session.tag", label: "Tag", title: "release train v0.24", state: "done", outcome: "Tag set.", via: "call", conversation: "c" },
  { ts: 1764100800, op: "project.color", label: "Project colour", title: "landing page", state: "stale", outcome: "", via: "typed", conversation: "c" },
];

for (const theme of ["dark", "light"] as const) {
  test(`authority: presets and class rows save, RISKY and locked shown · ${theme}`, async ({ page }, info) => {
    const saves = await authority(page, ASK);
    await page.goto("/settings/ai-hq");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    const section = page.getByTestId("hq-authority");
    await expect(section.getByTestId("hq-authority-preset-ask_first")).toHaveAttribute("aria-checked", "true");
    for (const c of ["destructive", "external", "widens"]) {
      await expect(section.getByTestId(`hq-authority-row-${c}`).getByText("RISKY")).toBeVisible();
    }
    await expect(section.getByTestId("hq-authority-row-reversible").getByText("RISKY")).toHaveCount(0);
    await expect(section.getByTestId("hq-authority-locked")).toContainText("This Authority setting");
    await expect(section.getByTestId("hq-acts-empty")).toBeVisible();
    if (PHONE(page)) {
      for (const b of await section.getByRole("radio").all()) {
        expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      }
    }
    const over = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(over).toBeLessThanOrEqual(0);
    await tall(page);
    await section.screenshot({ path: info.outputPath(`hq-authority-ask-first-${theme}.png`) });

    await section.getByTestId("hq-authority-preset-standard").click();
    await expect.poll(() => saves.length).toBe(1);
    expect(saves[0]).toEqual({ revision: 4, preset: "standard" });
    await expect(section.getByTestId("hq-authority-preset-standard")).toHaveAttribute("aria-checked", "true");
    await expect(section.getByRole("status")).toContainText("Saved");

    // The operator's own mix (#2070 direction): starts or sends acts too, which A1b (#2391) runs.
    const starts = section.getByTestId("hq-authority-row-starts");
    await starts.getByRole("radio", { name: "Act" }).click();
    await expect.poll(() => saves.length).toBe(2);
    expect(saves[1]).toEqual({ revision: 5, classes: { starts: "act" } });
    await expect(section.getByTestId("hq-authority-custom")).toBeVisible();
    await expect(section.getByTestId("hq-authority-later-starts")).toHaveCount(0);
    await expect(section.getByTestId("hq-authority-later-destructive")).toBeVisible();
    await section.screenshot({ path: info.outputPath(`hq-authority-custom-${theme}.png`) });
  });
}

for (const theme of ["dark", "light"] as const) {
  test(`authority: a stale revision says so and Reload reads it again · ${theme}`, async ({ page }, info) => {
    const saves = await authority(page, ASK, { conflict: true });
    await page.goto("/settings/ai-hq");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    const section = page.getByTestId("hq-authority");
    await section.getByTestId("hq-authority-row-reversible").getByRole("radio", { name: "Act" }).click();
    await expect.poll(() => saves.length).toBe(1);
    await expect(section.getByRole("alert")).toContainText("changed elsewhere");
    await expect(section.getByTestId("hq-authority-preset-full")).toBeDisabled();
    await tall(page);
    await section.screenshot({ path: info.outputPath(`hq-authority-stale-${theme}.png`) });
    await section.getByRole("button", { name: "Reload" }).click();
    await expect(section.getByTestId("hq-authority-preset-full")).toBeEnabled();
  });
}

for (const theme of ["dark", "light"] as const) {
  test(`authority: recent acts list · ${theme}`, async ({ page }, info) => {
    await authority(page, { ...ASK, reversible: "act" }, { acts: ACTS });
    await page.goto("/settings/ai-hq");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    const list = page.getByTestId("hq-acts");
    await expect(list.getByRole("listitem")).toHaveCount(3);
    await expect(list).toContainText("Rename · docs-site: rewrite the install guide");
    await expect(list).toContainText("on a call");
    await tall(page);
    await page.getByTestId("hq-authority").screenshot({ path: info.outputPath(`hq-authority-standard-acts-${theme}.png`) });
  });

  test(`a card HQ ran itself reads Done by HQ, with no buttons · ${theme}`, async ({ page }, info) => {
    await shell(page, {
      action_id: PID,
      kind: "operation",
      op: "session.rename",
      risk: "reversible",
      label: "Rename",
      verb: "Rename",
      title: "docs-site: rewrite the install guide",
      text: "Renames it to “Install guide v2”.",
      needs: [],
      state: "done",
      summary: "Renamed.",
      reason: "",
      can_confirm: false,
      can_dismiss: false,
      decided_by: "hq",
    });
    const c = await openCard(page);
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    await expect(c.getByTestId("ask-operation-kicker")).toHaveText("Done by HQ");
    await expect(c.getByTestId("ask-operation-by-hq")).toContainText("Authority setting");
    await expect(c.getByRole("button")).toHaveCount(0);
    await c.screenshot({ path: info.outputPath(`hq-act-card-${theme}.png`) });
  });
}

// ---- A1b (#2391): starts or sends --------------------------------------------------------------

const STARTS: Classes = { ...ASK, reversible: "act", starts: "act" };

for (const theme of ["dark", "light"] as const) {
  test(`authority: the untrusted-text switch is labelled off and RISKY · ON, keyboard-reachable · ${theme}`, async ({ page }, info) => {
    const saves = await authority(page, STARTS);
    await page.goto("/settings/ai-hq");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    const section = page.getByTestId("hq-authority");
    const row = section.getByTestId("hq-authority-untrusted-starts");
    const sw = row.getByRole("switch");
    await expect(sw).not.toBeChecked();
    await expect(row).toContainText("Off: HQ asks first then.");
    await expect(row.getByText("RISKY", { exact: true })).toBeVisible();
    // Only the class that has one draws it.
    await expect(section.getByTestId("hq-authority-untrusted-reversible")).toHaveCount(0);
    // The whole labelled row is the target; on a phone that is at least 44 px.
    const box = (await row.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    if (PHONE(page)) expect(box.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    const over = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(over).toBeLessThanOrEqual(0);
    await tall(page);
    await section.screenshot({ path: info.outputPath(`hq-authority-a1b-untrusted-off-${theme}.png`) });

    // The keyboard reaches it and Space switches it: one CAS write of exactly this switch.
    await sw.focus();
    await expect(sw).toBeFocused();
    const ring = await sw.evaluate((el) => getComputedStyle(el).outlineStyle);
    expect(ring).not.toBe("none");
    await page.keyboard.press("Space");
    await expect.poll(() => saves.length).toBe(1);
    expect(saves[0]).toEqual({ revision: 4, untrusted: { starts: true } });
    await expect(sw).toBeChecked();
    await expect(row.getByText("RISKY · ON", { exact: true })).toBeVisible();
    await expect(row).toContainText("A hostile report can now get HQ to start or send.");
    await section.screenshot({ path: info.outputPath(`hq-authority-a1b-untrusted-on-${theme}.png`) });

    // A tap switches it back off, against the next revision.
    await row.click();
    await expect.poll(() => saves.length).toBe(2);
    expect(saves[1]).toEqual({ revision: 5, untrusted: { starts: false } });
    await expect(sw).not.toBeChecked();
  });

  test(`authority: a stale switch save says so and Reload reads the current setting · ${theme}`, async ({ page }, info) => {
    const saves = await authority(page, STARTS, { conflict: true });
    await page.goto("/settings/ai-hq");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    const section = page.getByTestId("hq-authority");
    const sw = section.getByTestId("hq-authority-untrusted-starts").getByRole("switch");
    await sw.click();
    await expect.poll(() => saves.length).toBe(1);
    await expect(section.getByRole("alert")).toContainText("changed elsewhere");
    await expect(sw).toBeDisabled();
    await expect(sw).not.toBeChecked();
    await tall(page);
    await section.screenshot({ path: info.outputPath(`hq-authority-a1b-stale-${theme}.png`) });
    await section.getByRole("button", { name: "Reload" }).click();
    await expect(sw).toBeEnabled();
    await expect(section.getByRole("alert")).toHaveCount(0);
  });
}

const SESSION_KEY = "claude-api:0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d";

async function openThread(page: Page, testId: string) {
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  const card = panel.getByTestId(testId);
  await expect(card).toBeVisible();
  return card;
}

for (const theme of ["dark", "light"] as const) {
  test(`a session HQ started reads Started by HQ, and its Open link opens it · ${theme}`, async ({ page }, info) => {
    await shell(page, {
      action_id: PID,
      kind: "session",
      title: "web-shop",
      agent: "Claude — API",
      text: "Add Polish as a UI language.",
      state: "started",
      engine: "claude-api",
      runtime: "api",
      bypass_default: false,
      bypass: false,
      session_key: SESSION_KEY,
      reason: "",
      notice: "",
      can_start: false,
      can_dismiss: false,
      decided_by: "hq",
    });
    const c = await openThread(page, "ask-session");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    await expect(c.getByTestId("ask-session-kicker")).toHaveText("Started by HQ");
    await expect(c.getByTestId("ask-session-by-hq")).toContainText("Authority setting");
    await expect(c.getByTestId("ask-session-bypass-used")).toContainText("Off");
    await expect(c.getByRole("button", { name: /Start/ })).toHaveCount(0);
    const openLink = c.getByTestId("ask-session-open");
    const box = (await openLink.boundingBox())!;
    if (PHONE(page)) expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await c.screenshot({ path: info.outputPath(`hq-a1b-started-${theme}.png`) });
    await openLink.click();
    await expect(page).toHaveURL(/\/s\/claude-api\/0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d/);
  });

  test(`a connector message HQ sent reads Sent by HQ · ${theme}`, async ({ page }, info) => {
    await shell(page, {
      action_id: PID,
      kind: "message",
      connector: "Matrix",
      recipient: "#deploys",
      recipient_kind: "room",
      recipient_members: 6,
      text: "Staging is green.",
      state: "sent",
      reason: "",
      code: "",
      can_send: false,
      can_dismiss: false,
      decided_by: "hq",
    });
    const c = await openThread(page, "ask-message");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    await expect(c.getByTestId("ask-message-kicker")).toHaveText("Sent by HQ");
    await expect(c).toContainText("#deploys");
    await expect(c.getByTestId("ask-message-outcome")).toContainText("Matrix accepted it.");
    await expect(c.getByRole("button")).toHaveCount(0);
    await c.screenshot({ path: info.outputPath(`hq-a1b-sent-${theme}.png`) });
  });

  test(`an automation HQ ran now reads Ran by HQ · ${theme}`, async ({ page }, info) => {
    await shell(page, {
      action_id: PID,
      kind: "operation",
      op: "automation.run_now",
      risk: "external",
      label: "Run now",
      verb: "Run now",
      title: "Nightly sweep",
      text: "Runs it once now, within exactly the scope you approved; on a paused one it runs once without resuming. Its run shows in Automations.",
      needs: [],
      state: "done",
      summary: "A run started (dispatching). Its outcome shows in Automations.",
      reason: "",
      can_confirm: false,
      can_dismiss: false,
      decided_by: "hq",
    });
    const c = await openCard(page);
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    await expect(c.getByTestId("ask-operation-kicker")).toHaveText("Ran by HQ");
    await expect(c.getByRole("button")).toHaveCount(0);
    await c.screenshot({ path: info.outputPath(`hq-a1b-ran-${theme}.png`) });
  });

  test(`a start after HQ read a report asks first and says why; Start still works · ${theme}`, async ({ page }, info) => {
    const fake = await shell(page, {
      action_id: PID,
      kind: "session",
      title: "web-shop",
      agent: "Claude — API",
      text: "Fix the flaky deploy test, as the report suggests.",
      state: "pending",
      engine: "claude-api",
      runtime: "api",
      bypass_default: false,
      bypass: null,
      session_key: "",
      reason: "HQ read text from a session or connector in this conversation, so this asks first",
      notice: "",
      can_start: true,
      can_dismiss: true,
    });
    const bodies: Record<string, unknown>[] = [];
    await page.route(`**/api/ask/proposals/${PID}/decide`, (r) => {
      bodies.push(r.request().postDataJSON() as Record<string, unknown>);
      Object.assign(fake.conversations[0].turns[0].proposals![0], {
        state: "deciding",
        can_start: false,
        can_dismiss: false,
      });
      return r.fulfill({ status: 202, json: { proposal: { state: "deciding" } } });
    });
    const c = await openThread(page, "ask-session");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    await expect(c.getByTestId("ask-session-kicker")).toHaveText("Asks first");
    await expect(c.getByTestId("ask-session-asks-first")).toContainText("so this asks first");
    const start = c.getByTestId("ask-session-start");
    if (PHONE(page)) expect((await start.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await c.screenshot({ path: info.outputPath(`hq-a1b-asks-first-${theme}.png`) });
    await start.click();
    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0]).toEqual({ decision: "start", bypass: false });
  });
}
