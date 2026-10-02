import { expect, test, type Locator, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

// #1009: usage-analytics consent, in a real browser on the desktop AND the mobile (Pixel 7)
// projects. The wizard asks between Tour and Launch; the choice is posted as `analytics_consent`,
// config is re-fetched after a successful save (that fetch is what lets a new install count on its
// first day), and a failed save keeps the step with the setting actually in effect. Settings has
// the same toggle, and the server's kill switch renders it disabled with the reason.

type Analytics = { enabled: boolean; decided: boolean; available: boolean };

async function mockApp(
  page: Page,
  opts: { onboarded: boolean; analytics: Analytics; failConsentSave?: boolean },
) {
  const state = { analytics: { ...opts.analytics }, onboarded: opts.onboarded };
  const log = { consentBodies: [] as unknown[], configFetches: 0, configAfterSave: 0 };

  await page.route("**/api/config", (r) => {
    log.configFetches += 1;
    if (log.consentBodies.length) log.configAfterSave += 1;
    return r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        onboarded: state.onboarded,
        analytics: state.analytics,
      },
    });
  });
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) =>
    r.fulfill({
      json: { engines: [{ id: "claude", present: true, supports_new: true, bin: "/x/claude" }] },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [{ cwd: "/home/u/battlelab", label: "battlelab" }] } }),
  );
  await page.route(/\/api\/fs\/dirs(\?.*)?$/, (r) =>
    r.fulfill({ json: { path: "/home/u", home: "/home/u", dirs: [] } }),
  );
  await page.route("**/api/prefs", async (r) => {
    const body = (r.request().postDataJSON() ?? {}) as {
      analytics_consent?: boolean;
      onboarded?: boolean;
    };
    if (body.onboarded) state.onboarded = true;
    if ("analytics_consent" in body) {
      log.consentBodies.push(body);
      if (opts.failConsentSave) {
        await r.fulfill({ status: 500, json: { detail: "boom" } });
        return;
      }
      state.analytics = {
        ...state.analytics,
        enabled: !!body.analytics_consent,
        decided: true,
      };
      await r.fulfill({ json: { analytics: state.analytics } });
      return;
    }
    await r.fulfill({ json: {} });
  });
  return log;
}

async function walkToAnalyticsStep(dialog: Locator) {
  await dialog.getByRole("button", { name: /get started/i }).click(); // → security
  await dialog.getByRole("button", { name: /skip.*continue/i }).click(); // → agents
  await expect(dialog.getByText("claude", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: /^next$/i }).click(); // → ai
  await dialog.getByRole("button", { name: /^next$/i }).click(); // → project
  await dialog.getByRole("button", { name: /^next$/i }).click(); // → tour
  for (let k = 0; k < 12; k++) {
    const next = dialog.getByRole("button", { name: /^next$/i });
    if (!(await next.isVisible())) break;
    await next.click();
  }
  await dialog.getByRole("button", { name: /finish tour/i }).click(); // → usage analytics
  await expect(dialog.getByRole("heading", { name: "Usage analytics" })).toBeVisible();
}

const UNDECIDED: Analytics = { enabled: false, decided: false, available: true };

test("a fresh install is asked, unticked; ticking posts true and lands on Launch (#1009)", async ({
  page,
}) => {
  const log = await mockApp(page, { onboarded: false, analytics: UNDECIDED });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: /set up battlelab/i });
  await expect(dialog).toBeVisible();
  await walkToAnalyticsStep(dialog);

  const box = dialog.getByRole("checkbox", { name: "Share usage analytics" });
  await expect(box).not.toBeChecked();
  await expect(
    dialog.getByText("Nothing is sent unless you tick the box and continue.", { exact: false }),
  ).toBeVisible();
  const cont = dialog.getByRole("button", { name: /^continue/i });
  // The §8 touch floor applies under coarse pointers and at ≤800px — the mobile project.
  if (test.info().project.name === "mobile") {
    await cont.scrollIntoViewIfNeeded();
    expect((await cont.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }

  await box.check();
  await cont.click();

  await expect(dialog.getByRole("heading", { name: /start your first session/i })).toBeVisible();
  expect(log.consentBodies).toEqual([{ analytics_consent: true }]);
  await expect.poll(() => log.configAfterSave).toBeGreaterThan(0);
});

test("continuing without ticking posts a no; a failed save keeps the step and names the setting in effect (#1009)", async ({
  page,
}) => {
  const log = await mockApp(page, {
    onboarded: false,
    analytics: UNDECIDED,
    failConsentSave: true,
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: /set up battlelab/i });
  await walkToAnalyticsStep(dialog);

  // The default-off path: the box is left alone, so the decision sent is a no.
  await expect(dialog.getByRole("checkbox", { name: "Share usage analytics" })).not.toBeChecked();
  await dialog.getByRole("button", { name: /^continue/i }).click();
  await expect.poll(() => log.consentBodies).toEqual([{ analytics_consent: false }]);

  const alert = dialog.getByRole("alert");
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("your previous setting (not set, so off) is still in effect");
  await expect(dialog.getByRole("heading", { name: "Usage analytics" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /^try again/i })).toBeVisible();
});

test("Settings toggles consent (#1009)", async ({
  page,
}) => {
  const log = await mockApp(page, { onboarded: true, analytics: UNDECIDED });
  await page.goto(settingsPath("analytics"), { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Usage analytics" })).toBeVisible();
  const box = page.getByRole("checkbox", { name: "Share usage analytics" });
  await expect(box).not.toBeChecked();
  // `click`, not `check`: the box shows what the SERVER holds, so it flips when the save answers.
  await box.click();
  await expect(box).toBeChecked();
  expect(log.consentBodies).toEqual([{ analytics_consent: true }]);
});

test("Settings shows the server's kill switch as a disabled toggle with the reason (#1009)", async ({
  page,
}) => {
  await mockApp(page, {
    onboarded: true,
    analytics: { enabled: true, decided: true, available: false },
  });
  await page.goto(settingsPath("analytics"), { waitUntil: "domcontentloaded" });
  const box = page.getByRole("checkbox", { name: "Share usage analytics" });
  await expect(box).toBeDisabled();
  await expect(box).not.toBeChecked();
  await expect(page.getByText(/Turned off for this server by AGENT_SESSIONS_ANALYTICS=0/)).toBeVisible();
});
