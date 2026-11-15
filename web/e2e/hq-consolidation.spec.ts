/** #2037 / #2029 P1: old links reach one HQ home and independent automation grants stay separate.
 * Real-browser navigation and saves on phone and desktop; the baseline must fail before the move. */
import { expect, test, type Page } from "@playwright/test";
import { mockShell } from "./callFixture";
import { TOUCH_FLOOR } from "./touchTarget";
import { MISSION, missionList, missionRow, mockMissions, openMissionRail } from "./mission-console";

async function fixture(page: Page) {
  const calls = await mockShell(page);
  const common = {
    enabled: true, autonomy: "suggest", allowed_verbs: ["continue"], auto_verbs_ceiling: ["continue"],
    confidence_min: 0.75, interval_minutes: 10, max_actions_per_pass: 4, proposal_ttl_minutes: 30,
    stale_hours: 24, nudge_template: "Continue.", default_nudge_template: "Continue.",
    notify: "escalations", configured: true,
  };
  const policies = {
    session: { ...common, revision: "session-1" },
    mission: { ...common, autonomy: "off", revision: "mission-1", auto_ai_directions: false,
      ai_direction_confidence_min: 0.9, ai_direction_confidence_floor: 0.9,
      ai_direction_confidence_max: 1, judge_confidence_min: 0.9, judge_confidence_floor: 0.9,
      judge_confidence_max: 1 },
  };
  const saves: { scope: string; body: Record<string, unknown> }[] = [];
  const legacy: unknown[] = [];
  let conflict = false;
  let failRead = false;
  let unavailable = false;
  const preferences: Record<string, unknown> = {
    pulse: { configured: true, auto_enabled: false, interval_minutes: 30, window_days: 3, scan_depth: "fast" },
    hq_contact: "off", hq_contact_rev: 0, hq_reports: false,
    hq_route: { enabled: true, model: "", thinking: "off" },
  };
  await page.route("**/api/config", (r) => failRead
    ? r.fulfill({ status: 503, json: { detail: "Config unreadable" } })
    : r.fulfill({ json: {
    csrf: "csrf-x", auth_mode: "none", username: null, terminal_backend: "ws", onboarded: true,
    new_session_engines: [], ai_review: { configured: true }, ...preferences,
    automation: { version: 1, legacy_compatible: false, ...policies },
  } }));
  await page.route("**/api/connectors", (r) => r.fulfill({ json: { catalog: [], instances: [] } }));
  await page.route("**/api/ai-review/models**", (r) => r.fulfill({ json: { models: ["model-a"] } }));
  await page.route("**/api/prefs", (r) => {
    const patch = r.request().postDataJSON();
    legacy.push(patch);
    for (const [key, value] of Object.entries(patch)) {
      preferences[key] = typeof value === "object" && value !== null
        ? { ...preferences[key] as object, ...value } : value;
    }
    if ("hq_contact" in patch) preferences.hq_contact_rev = Number(preferences.hq_contact_rev) + 1;
    return r.fulfill({ json: preferences });
  });
  await page.route("**/api/automation/*", (r) => {
    const scope = r.request().url().split("/").at(-1) as "session" | "mission";
    const body = r.request().postDataJSON();
    saves.push({ scope, body });
    if (unavailable) return r.fulfill({ status: 503, json: { detail: "Policy unreadable" } });
    if (conflict || body.revision !== policies[scope].revision) {
      conflict = false;
      policies[scope] = { ...policies[scope], autonomy: "suggest", revision: `${scope}-new` };
      return r.fulfill({ status: 409, json: { detail: "Automation policy changed; reload it." } });
    }
    policies[scope] = { ...policies[scope], ...body.policy, revision: `${scope}-${saves.length + 1}` };
    return r.fulfill({ json: { version: 1, scope, policy: policies[scope] } });
  });
  return { saves, legacy, policies, preferences, calls, conflict: () => { conflict = true; },
    failRead: (value: boolean) => { failRead = value; }, unavailable: (value: boolean) => { unavailable = value; } };
}

test("legacy settings links reach the matching section in HQ", async ({ page }) => {
  await fixture(page);
  for (const [old, anchor] of [
    ["ai-mission-control", "orch-h"], ["ai-hq-calls", "hq-calls"],
    ["ai-hq-connectors", "hq-connectors"], ["appearance#hq-contact", "hq-contact"],
  ]) {
    await page.goto(`/settings/${old}`);
    await expect(page).toHaveURL(new RegExp(`/settings/ai-hq#${anchor}$`));
    await expect(page.locator(`#${anchor}`)).toBeVisible();
  }
});

test("session and mission saves use their own revisions and a stale save reloads", async ({ page }) => {
  const f = await fixture(page);
  await page.goto("/settings/ai-hq#orch-h");
  const session = page.getByRole("region", { name: "Session assistance", exact: true });
  const mission = page.getByRole("region", { name: "Mission orchestration", exact: true });
  await expect(session.getByLabel("Autonomy", { exact: true })).toHaveValue("suggest");
  await expect(mission.getByLabel("Autonomy", { exact: true })).toHaveValue("off");
  await session.getByLabel("Autonomy", { exact: true }).selectOption("off");
  await expect.poll(() => f.saves.length).toBe(1);
  expect(f.saves[0]).toEqual({ scope: "session", body: {
    revision: "session-1", policy: { autonomy: "off" },
  } });
  expect(f.policies.mission.revision).toBe("mission-1");
  await session.getByLabel("Notify me about").selectOption("all");
  await expect.poll(() => f.saves.length).toBe(2);
  expect(f.saves[1].body.revision).toBe("session-2");
  f.conflict();
  await mission.getByLabel("Autonomy", { exact: true }).selectOption("yolo");
  await expect(mission.getByRole("alert")).toContainText("changed elsewhere");
  await expect(mission.getByLabel("Autonomy", { exact: true })).toHaveValue("suggest");
  expect(f.saves).toHaveLength(3); // The rejected YOLO edit is never replayed after reload.
  await mission.getByLabel("Autonomy", { exact: true }).selectOption("off");
  await expect.poll(() => f.saves.length).toBe(4);
  expect(f.saves[3].body.revision).toBe("mission-new");
  expect(f.legacy).toEqual([]);
  await expect(session.getByLabel("Autonomy", { exact: true })).toHaveValue("off");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("an unreadable policy disables its editor until a successful reload", async ({ page }) => {
  const f = await fixture(page);
  f.policies.mission.revision = "invalid";
  await page.goto("/settings/ai-hq#orch-h");
  const mission = page.getByRole("region", { name: "Mission orchestration", exact: true });
  await expect(mission.getByLabel("Autonomy", { exact: true })).toBeDisabled();
  await expect(mission.getByRole("alert")).toContainText("could not read");
  f.policies.mission.revision = "mission-readable";
  await mission.getByRole("button", { name: "Reload settings" }).click();
  await expect(mission.getByLabel("Autonomy", { exact: true })).toBeEnabled();
  await mission.getByLabel("Autonomy", { exact: true }).selectOption("suggest");
  await expect.poll(() => f.saves.length).toBe(1);
  expect(f.saves[0].body.revision).toBe("mission-readable");
});

test("HQ opens Missions and legacy mission links keep the HQ navigation", async ({ page }) => {
  await fixture(page);
  await page.goto("/hq");
  await page.getByRole("navigation", { name: "HQ views" }).getByRole("link", { name: "Missions", exact: true }).click();
  await expect(page).toHaveURL(/\/mission$/);
  await expect(page.getByRole("navigation", { name: "HQ views" })).toBeVisible();
  await page.goto("/pulse");
  await expect(page).toHaveURL(/\/mission$/);
  await page.getByRole("navigation", { name: "HQ views" }).getByRole("link", { name: "Conversations" }).click();
  await expect(page).toHaveURL(/\/hq$/);
  await expect(page.getByTestId("hq-page")).toBeVisible();
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 });
    const views = page.getByRole("navigation", { name: "HQ views" }).getByRole("link");
    // Wait for the responsive route frame before measuring its links.
    await expect.poll(() => views.evaluateAll((links, floor) =>
      links.length === 3 && links.every((link) => link.getBoundingClientRect().height >= floor),
    TOUCH_FLOOR)).toBe(true);
    const boxes = await views.evaluateAll((links) => links.map((link) => {
      const { x, y, width, height } = link.getBoundingClientRect();
      return { x, y, width, height };
    }));
    expect(boxes).toHaveLength(3);
    for (const box of boxes) {
      expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      expect(Math.abs(box.y - boxes[0].y)).toBeLessThanOrEqual(1);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
    }
  }
});

test("a 503 and a failed conflict refresh require a successful reload before editing", async ({ page }) => {
  const f = await fixture(page);
  await page.goto("/settings/ai-hq#orch-h");
  const session = page.getByRole("region", { name: "Session assistance", exact: true });
  const tier = session.getByLabel("Autonomy", { exact: true });
  f.unavailable(true);
  await tier.selectOption("yolo");
  await expect(session.getByRole("alert")).toContainText("could not read or update");
  await expect(tier).toBeDisabled();
  f.failRead(true);
  await session.getByRole("button", { name: "Reload settings" }).click();
  await expect(tier).toBeDisabled();
  f.unavailable(false);
  f.failRead(false);
  await session.getByRole("button", { name: "Reload settings" }).click();
  await expect(tier).toBeEnabled();
  f.conflict();
  f.failRead(true);
  await tier.selectOption("yolo");
  await expect(session.getByRole("alert")).toContainText("could not read the latest values");
  await expect(tier).toBeDisabled();
  expect(f.saves).toHaveLength(2);
  f.failRead(false);
  await session.getByRole("button", { name: "Reload settings" }).click();
  await expect(tier).toBeEnabled();
  await tier.selectOption("off");
  await expect.poll(() => f.saves.length).toBe(3);
  expect(f.saves[2].body.revision).toBe("session-new");
  expect(f.legacy).toEqual([]);
});

test("every settings group is reachable and moved controls keep their existing saves", async ({ page }) => {
  const f = await fixture(page);
  await page.goto("/settings/ai-hq");
  const groups = page.getByRole("navigation", { name: "HQ settings groups" });
  for (const [name, id] of [
    ["Contact", "hq-contact"], ["HQ models", "hq-models-h"], ["Question routing", "hq-route-h"], ["Autonomy", "orch-h"],
    ["Reports", "hq-reports-h"], ["Session scan", "pulse-h"], ["Mission checks", "hq-forge"],
    ["Calls", "hq-calls"], ["Connectors", "hq-connectors"],
  ]) {
    const link = groups.getByRole("link", { name, exact: true });
    await link.click();
    await expect(page.locator(`#${id}`)).toBeInViewport();
    expect((await link.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await page.getByRole("radio", { name: /^Random/ }).click();
  await expect.poll(() => f.preferences.hq_contact).toBe("random");
  await page.getByTestId("hq-route-model").selectOption("model-a");
  await expect.poll(() => f.preferences.hq_route).toEqual({ enabled: true, model: "model-a", thinking: "off" });
  await page.getByTestId("hq-reports-toggle").click();
  await expect.poll(() => f.preferences.hq_reports).toBe(true);
  await page.getByLabel("Scan depth").selectOption("slow");
  await expect.poll(() => (f.preferences.pulse as { scan_depth: string }).scan_depth).toBe("slow");
  await page.getByTestId("forge-base-url").fill("https://forge.example");
  await page.getByRole("button", { name: "Save connection", exact: true }).click();
  await expect.poll(() => (f.preferences.forge as { base_url: string })?.base_url).toBe("https://forge.example");
  await page.getByRole("combobox", { name: "Speed for General Jessica Hale" }).selectOption("1.05");
  await expect.poll(() => f.calls.saves.length).toBe(1);
  expect(f.calls.saves[0]).toEqual({ contacts: { "us-woman": { speed: 1.05 } } });
  expect(f.saves).toEqual([]);
  expect(f.legacy.some((patch) => "orchestrator" in (patch as object))).toBe(false);
});

for (const theme of ["dark", "light"]) {
  test(`${theme} HQ settings and Missions fit desktop and phone`, async ({ page, isMobile }, info) => {
    const f = await fixture(page);
    await page.setViewportSize({ width: isMobile ? 390 : 1280, height: isMobile ? 844 : 900 });
    await page.addInitScript((choice) => localStorage.setItem("tr-theme", choice), theme);
    const mid = "msn_" + "b".repeat(32);
    await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [{ id: "p1", name: "BattleLab", folders: ["/example"] }] } }));
    await mockMissions(page, { missions: () => missionList([missionRow({ id: mid, title: "Review the release", state: "running" })]),
      mission: { ...MISSION, id: mid, title: "Review the release", events: [], events_next_seq: null } });
    await page.goto("/settings/ai-hq");
    await expect(page.getByTestId("hq-gallery")).toBeVisible();
    await page.evaluate(async () => {
      await document.fonts.ready;
      await Promise.all(Array.from(document.images, (img) => img.decode().catch(() => {})));
    });
    await page.screenshot({ path: info.outputPath(`settings-${theme}.png`) });
    await page.getByRole("navigation", { name: "HQ settings groups" }).getByRole("link", { name: "Autonomy" }).click();
    await expect(page.locator("#session-orch-h")).toBeInViewport();
    await page.screenshot({ path: info.outputPath(`autonomy-${theme}.png`) });
    f.policies.mission.revision = "invalid";
    await page.reload();
    await expect(page.getByRole("region", { name: "Mission orchestration", exact: true }).getByLabel("Autonomy", { exact: true })).toBeDisabled();
    await page.locator("#mission-orch-h").evaluate((el) => el.scrollIntoView({ block: "start" }));
    await expect(page.getByRole("region", { name: "Mission orchestration", exact: true }).getByRole("alert")).toBeInViewport();
    await page.screenshot({ path: info.outputPath(`unavailable-${theme}.png`) });
    await page.goto("/mission");
    await expect(page.getByRole("navigation", { name: "HQ views" })).toBeVisible();
    await page.screenshot({ path: info.outputPath(`missions-${theme}.png`) });
    await openMissionRail(page);
    await expect(page.getByTestId("rail-mission")).toHaveCount(1);
    await page.screenshot({ path: info.outputPath(`mission-list-${theme}.png`) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
