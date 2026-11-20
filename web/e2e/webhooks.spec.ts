/** Library → Webhooks (#2221 P1), in a real browser on desktop and phone, both themes.
 *
 *  A webhook is a name, an address and a secret. Making one shows the secret ONCE (copy secret, copy
 *  address, the "shown once" warning) and it is gone after leaving — never in storage, router state
 *  or a later read. A webhook's page shows the address, the secret's status, the last delivery as
 *  field NAMES and TYPES only, who uses it; Rotate shows a new secret once and says the old one works
 *  24 h more; Delete is off, with its reason, while an automation uses it. A review loop picks a
 *  webhook from the list and saves `webhook_id`; "+ New webhook…" makes one and comes back with the
 *  draft intact. Every control holds 44 px and nothing clips at 390 px. The API is the stateful
 *  fakes in `./automations.ts` and `./webhooks.ts`; what the page SENT is asserted. */
import { expect, test, type Page } from "@playwright/test";

import {
  AUTOMATION_NEW_PATH,
  WEBHOOKS_PATH,
  WEBHOOK_NEW_PATH,
  automationPath,
  webhookPath,
} from "../src/lib/routes";
import { automation, mockAutomations } from "./automations";
import { animationsSettled } from "./nightjarShell";
import { ROSTER, mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";
import { PR_SHAPE, mockWebhooks, webhook, type FakeHook } from "./webhooks";

const API = ROSTER.engines.find((e) => e.runtime === "api" && (e.status ?? "active") === "active")!.id;
const T = Math.floor(Date.now() / 1000);

const SHOP = webhook({
  id: "0a1b2c3d4e5f6a7b",
  name: "shop repo",
  last_delivery_at: T - 120,
  last_event: "pull_request",
  last_note: "opened",
  used_by: [{ id: "rv2", name: "Review every PR" }],
  last_shape: PR_SHAPE,
});
const DEPLOYS = webhook({
  id: "1b2c3d4e5f6a7b8c",
  name: "deploy notices",
  last_delivery_at: T - 3 * 3600,
  last_event: "workflow_run",
  unsigned_24h: 2,
});
const STATUS = webhook({ id: "2c3d4e5f6a7b8c9d", name: "status page" });
const LEGACY = webhook({
  id: "rv1",
  name: "Review loop · example-org/shop",
  legacy_automation: "rv1",
  used_by: [{ id: "rv1", name: "Review loop · example-org/shop" }],
  last_delivery_at: T - 600,
  last_event: "pull_request_review_approved",
});

const SPECIALISTS = [
  {
    id: "shop-reviewer",
    revision: 1,
    archived: false,
    current: { version: 3, title: "Shop Reviewer", summary: "", skills: [], engine: API, model: "m", effort: "high", capacity: 2, created_at: 1 },
    grade: "strong",
    live: 0,
    skills_missing: [],
    skills_outdated: [],
  },
];
const LOOP_ACTION = {
  kind: "forge_review_loop",
  project_id: "p1",
  repo: "example-org/shop",
  reviewer: { specialist_id: "shop-reviewer", version: 3 },
  reviewer_login: "shop-reviewer-bot",
  legs: { issues: true, pulls: true },
  max_rounds: { issue: 5, pr: 4 },
  return_to_origin: true,
};

async function setup(page: Page, hooks: FakeHook[] = [], automations: Record<string, unknown>[] = []) {
  const fake = await mockAutomations(page, { engines: [{ id: API, ok: true, reason: "" }], automations });
  await mockRoster(page);
  await page.route("**/api/specialists", (r) => r.fulfill({ json: { specialists: SPECIALISTS, templates: [] } }));
  const hooksFake = await mockWebhooks(page, hooks);
  return { fake, hooks: hooksFake };
}

/** Every control in `root` holds 44px; nothing passes the right edge; nothing scrolls sideways. */
async function targetsAndOverflow(page: Page, root: string) {
  const report = await page.evaluate((sel) => {
    const scope = document.querySelector(sel)!;
    const small: string[] = [];
    for (const el of scope.querySelectorAll<HTMLElement>("button, select, input, a")) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || getComputedStyle(el).visibility === "hidden") continue;
      // An inline link in running text (the breadcrumb, a hint) is not a target of its own.
      if (el.tagName === "A" && el.closest("p, dd, .nj-kicker, [role=status], [role=alert]")) continue;
      if (r.height < 44 || (el.tagName === "BUTTON" && r.width < 44))
        small.push(`${el.tagName} "${(el.textContent || "").trim().slice(0, 30)}" ${Math.round(r.width)}×${Math.round(r.height)}`);
    }
    const clipped: string[] = [];
    for (const el of scope.querySelectorAll<HTMLElement>("code, h1, [data-testid]")) {
      const r = el.getBoundingClientRect();
      if (r.width && r.right > document.documentElement.clientWidth + 0.5)
        clipped.push(`${el.tagName}.${el.dataset.testid ?? ""} right=${Math.round(r.right)}`);
    }
    const doc = document.scrollingElement!;
    const pageEl = scope as HTMLElement;
    return {
      small,
      clipped,
      docOverflow: doc.scrollWidth - doc.clientWidth,
      pageOverflow: pageEl.scrollWidth - pageEl.clientWidth,
    };
  }, root);
  expect(report.small, "controls under 44px").toEqual([]);
  expect(report.clipped, "elements past the right edge").toEqual([]);
  expect(report.docOverflow, "document overflows horizontally").toBeLessThanOrEqual(0);
  expect(report.pageOverflow, "the page overflows horizontally").toBeLessThanOrEqual(0);
}

/** Whether `secret` is anywhere the browser keeps state past this page. */
async function secretKept(page: Page, secret: string) {
  return page.evaluate((s) => {
    const stores = [localStorage, sessionStorage].flatMap((st) =>
      Array.from({ length: st.length }, (_, i) => `${st.key(i)}=${st.getItem(st.key(i)!)}`),
    );
    return [...stores, JSON.stringify(history.state ?? null)].some((v) => v.includes(s));
  }, secret);
}

test("the list: name, last delivery, who uses it, last event, unsigned count; a legacy one names its loop (#2221)", async ({ page }) => {
  await setup(page, [SHOP, DEPLOYS, STATUS, LEGACY]);
  await page.goto(WEBHOOKS_PATH);
  await expect(page.getByRole("heading", { level: 1, name: "Webhooks" })).toBeVisible();
  const rows = page.getByTestId("webhook-row");
  await expect(rows).toHaveCount(4);
  const row = (id: string) => page.locator(`[data-testid="webhook-row"][data-id="${id}"]`);
  await expect(row(SHOP.id)).toContainText("shop repo");
  await expect(row(SHOP.id).getByTestId("webhook-row-meta")).toHaveText(
    "used by 1 automation · last: pull_request · 0 unsigned in 24 h",
  );
  await expect(row(DEPLOYS.id).getByTestId("webhook-row-meta")).toHaveText(
    "not used yet · last: workflow_run · 2 unsigned in 24 h",
  );
  await expect(row(STATUS.id).getByTestId("webhook-row-last")).toHaveText("no delivery yet");
  await expect(row(STATUS.id).getByTestId("webhook-row-meta")).toHaveText("not used yet");
  await expect(row(LEGACY.id).getByTestId("webhook-row-meta")).toContainText(
    "old address · used by Review loop · example-org/shop",
  );
  // The section lights Library → Webhooks.
  const bar = page.locator(".hud-topbar");
  await expect(bar.locator('[data-section="library"]')).toHaveAttribute("aria-current", "true");
  await row(SHOP.id).click();
  await expect(page).toHaveURL(new RegExp(`${webhookPath(SHOP.id)}$`));
});

test("create: a name only, then the address and the secret ONCE — copy both, and it is gone after leaving (#2221)", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => undefined);
  const { hooks } = await setup(page);
  await page.goto(WEBHOOKS_PATH);
  await expect(page.getByTestId("webhooks-empty")).toBeVisible();
  await page.getByTestId("webhook-new").click();
  await expect(page).toHaveURL(new RegExp(`${WEBHOOK_NEW_PATH}$`));
  // An empty name is refused here, before the server is asked.
  await page.getByTestId("webhook-create").click();
  await expect(page.locator("#webhook-new-name-hint")).toHaveText("Give it a name");
  expect(hooks.requests.filter((r) => r.method === "POST")).toHaveLength(0);

  await page.getByTestId("webhook-new-name").fill("  shop repo ");
  await page.getByTestId("webhook-create").click();
  const secret = page.getByTestId("webhook-secret-value");
  await expect(secret).toHaveText(/^[0-9a-f]{64}$/);
  const value = (await secret.textContent())!;
  expect(value).toBe(hooks.secrets[0]);
  expect(hooks.requests.find((r) => r.method === "POST")!.body).toEqual({ name: "shop repo" });
  const id = hooks.hooks[0].id;
  const origin = new URL(page.url()).origin;
  await expect(page.getByTestId("webhook-url")).toHaveText(`${origin}/hooks/w/${id}`);
  await expect(page.getByTestId("webhook-status")).toHaveText("waiting for first delivery");
  await expect(page.getByTestId("webhook-secret")).toContainText(/shown once — copy it now/i);
  await expect(page.getByTestId("webhook-last")).toContainText("No delivery yet.");

  await page.getByTestId("webhook-copy-secret").click();
  await expect(page.getByTestId("webhook-copied")).toBeVisible();
  if ((await page.getByTestId("webhook-copied").textContent())?.includes("copied"))
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(value);
  await page.getByTestId("webhook-copy-address-once").click();
  await expect(page.getByTestId("webhook-copied")).toBeVisible();
  if ((await page.getByTestId("webhook-copied").textContent())?.includes("copied"))
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(`${origin}/hooks/w/${id}`);
  // Held in the page alone: not in storage, not in history.
  expect(await secretKept(page, value)).toBe(false);

  // Leaving forgets it: the webhook's page never shows it, and back is the empty form again.
  await page.getByTestId("webhook-open").click();
  await expect(page.getByTestId("webhook-detail")).toBeVisible();
  await expect(page.getByTestId("webhook-secret-state")).toContainText("never shown again");
  await expect(page.locator("body")).not.toContainText(value);
  await page.goBack();
  await expect(page.getByTestId("webhook-new-name")).toBeVisible();
  await expect(page.getByTestId("webhook-secret-value")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(value);
  expect(await secretKept(page, value)).toBe(false);
  // Exactly one create; the secret was never asked for again.
  expect(hooks.requests.filter((r) => r.method !== "GET").map((r) => `${r.method} ${r.path}`)).toEqual(["POST "]);
});

test("a webhook's page: names and types (never values), rotate shows a new secret once, delete off while used (#2221)", async ({ page }) => {
  const { hooks } = await setup(page, [SHOP]);
  await page.goto(webhookPath(SHOP.id));
  await expect(page.getByTestId("webhook-name")).toHaveText("shop repo");
  const origin = new URL(page.url()).origin;
  await expect(page.getByTestId("webhook-url")).toHaveText(`${origin}/hooks/w/${SHOP.id}`);
  await expect(page.getByTestId("webhook-secret-state")).toContainText(/^set .* · never shown again$/);
  await expect(page.getByTestId("webhook-last")).toContainText("pull_request · opened");
  // The shape: exactly the field names and their types, in order.
  await expect(page.getByTestId("webhook-field")).toHaveText(PR_SHAPE.map((f) => `${f.path} · ${f.type}`));
  // Used by: a link to the automation.
  await expect(page.getByTestId("webhook-user")).toHaveText("Review every PR");
  await expect(page.getByTestId("webhook-user")).toHaveAttribute("href", automationPath("rv2"));

  // Delete is off, with its reason; the server is never asked.
  await expect(page.getByTestId("webhook-delete")).toBeDisabled();
  await expect(page.getByTestId("webhook-delete-why")).toContainText("used by 1 automation");

  // Rotate: a new secret, shown once; the old one keeps working 24 h more.
  await page.getByTestId("webhook-rotate").click();
  const secret = page.getByTestId("webhook-secret-value");
  await expect(secret).toHaveText(/^[0-9a-f]{64}$/);
  const value = (await secret.textContent())!;
  await expect(page.getByTestId("webhook-secret")).toContainText("the old one keeps working until");
  await expect(page.getByTestId("webhook-previous")).toContainText("keeps working until");
  expect(await secretKept(page, value)).toBe(false);
  await page.getByTestId("webhook-secret-done").click();
  await expect(secret).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(value);

  // Rename, fenced by revision.
  await page.getByTestId("webhook-rename").click();
  await page.getByTestId("webhook-rename-input").fill("shop repository");
  await page.getByTestId("webhook-rename-save").click();
  await expect(page.getByTestId("webhook-name")).toHaveText("shop repository");
  const patch = hooks.requests.find((r) => r.method === "PATCH")!;
  expect(patch.body).toEqual({ name: "shop repository", revision: 2 });
  expect(hooks.requests.filter((r) => r.method === "DELETE")).toHaveLength(0);
});

test("an unused webhook deletes after a confirm; no delivery yet says so (#2221)", async ({ page }) => {
  const { hooks } = await setup(page, [STATUS]);
  await page.goto(webhookPath(STATUS.id));
  await expect(page.getByTestId("webhook-last")).toHaveText("No delivery yet. Send a test from your service.");
  await expect(page.getByTestId("webhook-shape")).toHaveCount(0);
  await expect(page.getByTestId("webhook-unused")).toBeVisible();
  await page.getByTestId("webhook-delete").click();
  await page.getByTestId("webhook-delete-confirm").click();
  await expect(page).toHaveURL(new RegExp(`${WEBHOOKS_PATH}$`));
  await expect(page.getByTestId("webhooks-empty")).toBeVisible();
  expect(hooks.requests.filter((r) => r.method === "DELETE").map((r) => r.path)).toEqual([`/${STATUS.id}`]);
});

test("a legacy webhook: its old address and its loop, rotate allowed, never deleted (#2221)", async ({ page }) => {
  await setup(page, [LEGACY]);
  await page.goto(webhookPath(LEGACY.id));
  await expect(page.getByTestId("webhook-status")).toHaveText("old address");
  await expect(page.getByTestId("webhook-legacy")).toContainText("Review loop · example-org/shop");
  const origin = new URL(page.url()).origin;
  await expect(page.getByTestId("webhook-url")).toHaveText(`${origin}/hooks/a/rv1`);
  await expect(page.getByTestId("webhook-rotate")).toBeEnabled();
  await expect(page.getByTestId("webhook-delete")).toBeDisabled();
  await expect(page.getByTestId("webhook-delete-why")).toContainText("own address");
});

test("a review loop picks a webhook from the list and saves webhook_id; + New webhook… comes back with the draft (#2221)", async ({ page }) => {
  const { fake, hooks } = await setup(page, [SHOP, LEGACY]);
  await page.goto(AUTOMATION_NEW_PATH);
  await page.getByTestId("trigger-kind").getByRole("button", { name: "Webhook" }).click();
  await expect(page.getByTestId("action-kind").getByRole("button", { name: "Review loop" })).toHaveAttribute("aria-pressed", "true");
  const pick = page.getByTestId("trigger-webhook");
  // Only the pickable webhook is offered — never the legacy loop's own — plus "+ New webhook…".
  await expect(pick.locator("option")).toHaveText(["Choose a webhook…", /^shop repo · last: /, "+ New webhook…"]);
  await expect(page.getByTestId("editor-problems")).toContainText("Pick a webhook");
  await expect(page.getByTestId("webhook-pick-hint")).toBeVisible();

  await page.getByTestId("automation-name").fill("Review every PR · shop");
  await page.getByTestId("loop-project").selectOption("p1");
  await page.getByTestId("loop-repo").fill("example-org/shop");
  await page.getByTestId("loop-reviewer").selectOption("shop-reviewer");
  await page.getByTestId("loop-login").fill("shop-reviewer-bot");

  // + New webhook… → the create flow, then back with the draft intact and the new one picked.
  await pick.selectOption({ label: "+ New webhook…" });
  await expect(page).toHaveURL(new RegExp(`${WEBHOOK_NEW_PATH}$`));
  await page.getByTestId("webhook-new-name").fill("shop events");
  await page.getByTestId("webhook-create").click();
  const value = (await page.getByTestId("webhook-secret-value").textContent())!;
  await page.getByTestId("webhook-use-in-automation").click();
  await expect(page).toHaveURL(new RegExp(`${AUTOMATION_NEW_PATH}$`));
  const made = hooks.hooks.find((h) => h.name === "shop events")!;
  await expect(page.getByTestId("automation-name")).toHaveValue("Review every PR · shop");
  await expect(page.getByTestId("loop-repo")).toHaveValue("example-org/shop");
  await expect(pick).toHaveValue(made.id);
  await expect(page.locator("body")).not.toContainText(value);
  expect(await secretKept(page, value)).toBe(false);
  // The side panel shows the picked webhook, read-only, linking to its page.
  const panel = page.getByTestId("shared-webhook-panel");
  await expect(panel.getByTestId("shared-webhook-link")).toHaveText("shop events");
  await expect(panel.getByTestId("webhook-copy-address")).toBeVisible();
  await expect(panel.getByTestId("webhook-make-secret")).toHaveCount(0);

  // Switch to the existing one, and save: the body names the webhook, never the legacy scheme.
  await pick.selectOption(SHOP.id);
  await expect(panel.getByTestId("shared-webhook-link")).toHaveText("shop repo");
  await expect(page.getByTestId("editor-problems")).toHaveCount(0);
  for (const id of ["trigger-webhook", "webhook-copy-address", "shared-webhook-open"]) {
    const box = await page.getByTestId(id).first().boundingBox();
    expect(box!.height, `${id} is a 44px target`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("is off until you enable it");
  const create = fake.requests.find((r) => r.method === "POST" && r.path === "")!;
  expect(create.body).toMatchObject({
    name: "Review every PR · shop",
    trigger: { kind: "webhook", webhook_id: SHOP.id },
    action: LOOP_ACTION,
  });
});

test("run history of a loop on a picked webhook: the webhook's name, address, last delivery and link; no secret controls (#2221)", async ({ page }) => {
  await setup(
    page,
    [SHOP],
    [
      automation({
        id: "rv2",
        name: "Review every PR",
        trigger: { kind: "webhook", webhook_id: SHOP.id },
        action: LOOP_ACTION,
        policy: { concurrency: "allow", max_concurrent: 3, max_runs_per_day: 48, pause_after_failures: 3, expires_at: null },
        next_run: null,
        webhook: { ...SHOP, last_shape: undefined, webhook_id: SHOP.id },
      }),
    ],
  );
  await page.goto(automationPath("rv2"));
  const panel = page.getByTestId("shared-webhook-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId("shared-webhook-link")).toHaveText("shop repo");
  await expect(panel.getByTestId("webhook-last")).toContainText("pull_request · opened");
  await expect(page.getByTestId("webhook-panel")).toHaveCount(0);
  await expect(page.getByTestId("webhook-make-secret")).toHaveCount(0);
  await panel.getByTestId("shared-webhook-open").click();
  await expect(page).toHaveURL(new RegExp(`${webhookPath(SHOP.id)}$`));
});

test("at 390px: 44px targets and nothing clipped — list, create with its secret, a page with a rotated secret (#2221)", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 860 });
  await setup(page, [SHOP, DEPLOYS, STATUS, LEGACY]);
  await page.goto(WEBHOOKS_PATH);
  await expect(page.getByTestId("webhook-row")).toHaveCount(4);
  await targetsAndOverflow(page, '[data-testid="webhooks-page"]');

  await page.goto(WEBHOOK_NEW_PATH);
  await page.getByTestId("webhook-new-name").fill("a webhook with a rather long name, to wrap");
  await page.getByTestId("webhook-create").click();
  await expect(page.getByTestId("webhook-secret-value")).toBeVisible();
  await targetsAndOverflow(page, '[data-testid="webhook-new-page"]');

  await page.goto(webhookPath(SHOP.id));
  await page.getByTestId("webhook-rotate").click();
  await expect(page.getByTestId("webhook-secret-value")).toBeVisible();
  await targetsAndOverflow(page, '[data-testid="webhook-detail"]');
});

for (const theme of ["dark", "light"] as const) {
  test(`captures the list, a new webhook and a webhook's page — ${theme} (#2221)`, async ({ page }, info) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    const mobile = info.project.name === "mobile";
    await page.setViewportSize(mobile ? { width: 390, height: 1500 } : { width: 1440, height: 1000 });
    await setup(page, [SHOP, DEPLOYS, STATUS, LEGACY]);
    const shot = async (name: string) => {
      await animationsSettled(page);
      await page.screenshot({ path: info.outputPath(`impl-${name}-${info.project.name}-${theme}.png`) });
    };
    await page.goto(WEBHOOKS_PATH);
    await expect(page.getByTestId("webhook-row")).toHaveCount(4);
    await shot("list");
    await page.goto(webhookPath(SHOP.id));
    await expect(page.getByTestId("webhook-field").first()).toBeVisible();
    await shot("detail");
    await page.goto(WEBHOOK_NEW_PATH);
    await page.getByTestId("webhook-new-name").fill("status page");
    await page.getByTestId("webhook-create").click();
    await expect(page.getByTestId("webhook-secret-value")).toBeVisible();
    await shot("create");
  });
}
