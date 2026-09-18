import { expect, test, type Locator, type Page, type Route } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

// #993 increment 1: Settings → Maintenance gains "Archive old missions" and "Prune". The network
// is mocked so the page renders without a backend; every assertion is on what a real browser
// lays out and dispatches — dry-run counts, the confirm naming its side effects, the result
// lines, the empty / error / busy states, recovery once a job ends, a category that could not be
// measured, and the ≥44 px hit area of every new action button.

const PRUNE_INFO = {
  categories: {
    stale_sockets: { items: 159, bytes: 38912 },
    archived_scrollback: { items: 312, bytes: 188743680 },
  },
  runner: null,
};
const MISSIONS_INFO = {
  eligible: 9,
  sessions: 23,
  live_sessions: 5,
  unresolved: ["m-unresolved"],
  runner: null,
};

async function baseMocks(page: Page) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        onboarded: true,
      },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/scrollback", (r) => r.fulfill({ json: { bytes: 0, files: 0 } }));
}

type Handler = (route: Route) => Promise<void> | void;

async function maintenanceMocks(
  page: Page,
  opts: {
    pruneGet?: Handler;
    prunePost?: Handler;
    missionsGet?: Handler;
    missionsPost?: Handler;
  } = {},
) {
  await page.route("**/api/maintenance/prune", async (route) => {
    const post = route.request().method() === "POST";
    const h = post ? opts.prunePost : opts.pruneGet;
    if (h) return h(route);
    return route.fulfill({ json: PRUNE_INFO });
  });
  await page.route(/\/api\/missions\/archive-older(\?.*)?$/, async (route) => {
    const post = route.request().method() === "POST";
    const h = post ? opts.missionsPost : opts.missionsGet;
    if (h) return h(route);
    return route.fulfill({ json: MISSIONS_INFO });
  });
}

const card = (page: Page, heading: string) =>
  page.locator("section", { has: page.getByRole("heading", { name: heading, exact: true }) });

async function expectHitArea(scope: Page | Locator, name: RegExp) {
  const box = await scope.getByRole("button", { name }).boundingBox();
  expect(box).not.toBeNull();
  expect(box!.height).toBeGreaterThanOrEqual(44);
}

test.beforeEach(async ({ page }) => {
  await baseMocks(page);
});

test("dry-run counts render and both confirms name their side effects (#993)", async ({
  page,
}) => {
  let prunedWith: unknown = null;
  let archivedWith: unknown = null;
  await maintenanceMocks(page, {
    prunePost: async (route) => {
      prunedWith = route.request().postDataJSON();
      await route.fulfill({
        json: {
          removed: 157,
          bytes_freed: 38400,
          skipped: [
            { category: "stale_sockets", reason: "a live session holds its lock", count: 2 },
          ],
          failed: [
            {
              category: "archived_scrollback",
              item: "claude:abc",
              reason: "PermissionError: Permission denied",
            },
          ],
          failed_total: 1,
        },
      });
    },
    missionsPost: async (route) => {
      archivedWith = route.request().postDataJSON();
      await route.fulfill({
        json: {
          archived: 8,
          sessions_archived: 22,
          terminals_stopped: 5,
          skipped: [
            { mission_id: "m-unresolved", reason: "mission m-unresolved has an unresolved turn" },
          ],
          failed: [{ mission_id: "m-2", session_key: "claude:abc", reason: "background agent" }],
        },
      });
    },
  });
  await page.goto(settingsPath("maintenance"));
  const prune = card(page, "Prune");

  await expect(page.getByTestId("prune-count-stale_sockets")).toHaveText("159 · 38 KB");
  await expect(page.getByTestId("prune-count-archived_scrollback")).toHaveText("312 · 180 MB");
  await expectHitArea(prune, /prune selected \(1\)/i);
  await prune.getByRole("button", { name: /prune selected \(1\)/i }).click();
  await expect(page.getByText(/permanently remove 159 items/i)).toBeVisible();
  await expect(page.getByText(/session history is not touched/i)).toBeVisible();
  await expectHitArea(prune, /confirm prune/i);
  await prune.getByRole("button", { name: /confirm prune/i }).click();
  await expect(page.getByText(/removed 157 items/i)).toBeVisible();
  await expect(
    page.getByText(/skipped 2 \(stale terminal sockets\): a live session holds its lock/i),
  ).toBeVisible();
  await expect(page.getByText(/couldn’t remove claude:abc/i)).toBeVisible();
  expect(prunedWith).toEqual({ categories: ["stale_sockets"] });

  const missions = card(page, "Archive old missions");
  await expectHitArea(missions, /archive old missions \(9\)/i);
  await missions.getByRole("button", { name: /archive old missions \(9\)/i }).click();
  await expect(
    missions.getByText(/5 live terminals will be stopped; transcripts are kept/i),
  ).toBeVisible();
  await expect(
    missions.getByText(/1 mission with an unresolved turn will be skipped/i),
  ).toBeVisible();
  await expectHitArea(missions, /confirm mission archive/i);
  await missions.getByRole("button", { name: /confirm mission archive/i }).click();
  await expect(
    missions.getByText(/archived 8 missions and 22 of their sessions; stopped 5 live terminals/i),
  ).toBeVisible();
  await expect(missions.getByText(/skipped mission m-unresolved/i)).toBeVisible();
  await expect(missions.getByText(/session claude:abc was not archived/i)).toBeVisible();
  expect(archivedWith).toEqual({ older_than_days: 30 });
});

test("empty states disable the actions (#993)", async ({ page }) => {
  await maintenanceMocks(page, {
    pruneGet: (route) =>
      route.fulfill({
        json: {
          categories: {
            stale_sockets: { items: 0, bytes: 0 },
            archived_scrollback: { items: 0, bytes: 0 },
          },
          runner: null,
        },
      }),
    missionsGet: (route) =>
      route.fulfill({
        json: { eligible: 0, sessions: 0, live_sessions: 0, unresolved: [], runner: null },
      }),
  });
  await page.goto(settingsPath("maintenance"));
  await expect(page.getByText("Nothing to prune right now.")).toBeVisible();
  await expect(page.getByRole("button", { name: /prune selected/i })).toBeDisabled();
  await expect(page.getByText(/no finished missions older than 30 days/i)).toBeVisible();
  await expect(page.getByRole("button", { name: /archive old missions \(0\)/i })).toBeDisabled();
});

test("a failed dry run shows an error with Retry, and nothing runs (#993)", async ({ page }) => {
  let pruneCalls = 0;
  await maintenanceMocks(page, {
    pruneGet: (route) => {
      pruneCalls += 1;
      return pruneCalls === 1
        ? route.fulfill({ status: 500, json: { detail: "boom" } })
        : route.fulfill({ json: PRUNE_INFO });
    },
    missionsGet: (route) => route.fulfill({ status: 500, json: { detail: "boom" } }),
  });
  await page.goto(settingsPath("maintenance"));
  await expect(page.getByText(/couldn’t measure the caches \(dry run failed\)/i)).toBeVisible();
  await expect(page.getByText(/couldn’t count missions \(dry run failed\)/i)).toBeVisible();
  const prune = card(page, "Prune");
  const refresh = prune.getByRole("button", { name: /refresh/i });
  const box = await refresh.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.height).toBeGreaterThanOrEqual(44);
  await refresh.click();
  await expect(page.getByTestId("prune-count-stale_sockets")).toHaveText("159 · 38 KB");
});

test("a busy runner refuses with retry copy rather than queueing (#993)", async ({ page }) => {
  await maintenanceMocks(page, {
    prunePost: (route) =>
      route.fulfill({
        status: 409,
        json: {
          detail:
            "Another maintenance job is running — unavailable; retry when maintenance finishes.",
          busy: { job: "missions", started_at: 1 },
        },
      }),
    missionsGet: (route) =>
      route.fulfill({ json: { ...MISSIONS_INFO, runner: { job: "prune", started_at: 1 } } }),
  });
  await page.goto(settingsPath("maintenance"));
  const missions = card(page, "Archive old missions");
  await expect(
    missions.getByText(
      /a maintenance job is running \(prune\) — unavailable; retry when maintenance finishes/i,
    ),
  ).toBeVisible();
  await expect(
    missions.getByRole("button", { name: /archive old missions \(9\)/i }),
  ).toBeDisabled();

  const prune = card(page, "Prune");
  await prune.getByRole("button", { name: /prune selected \(1\)/i }).click();
  await prune.getByRole("button", { name: /confirm prune/i }).click();
  await expect(
    prune.getByText(
      /a maintenance job is running \(missions\) — unavailable; retry when maintenance finishes/i,
    ),
  ).toBeVisible();
});

test("the cards recover on their own once the running job ends (#993)", async ({ page }) => {
  // The first dry run sees another job; the next one does not. Nothing is remounted — the card
  // polls while busy and re-enables itself.
  let pruneCalls = 0;
  let missionCalls = 0;
  await maintenanceMocks(page, {
    pruneGet: (route) => {
      pruneCalls += 1;
      return route.fulfill({
        json:
          pruneCalls === 1 ? { ...PRUNE_INFO, runner: { job: "missions", started_at: 1 } } : PRUNE_INFO,
      });
    },
    missionsGet: (route) => {
      missionCalls += 1;
      return route.fulfill({
        json:
          missionCalls === 1
            ? { ...MISSIONS_INFO, runner: { job: "prune", started_at: 1 } }
            : MISSIONS_INFO,
      });
    },
  });
  await page.goto(settingsPath("maintenance"));
  const prune = card(page, "Prune");
  const missions = card(page, "Archive old missions");
  await expect(prune.getByRole("button", { name: /prune selected/i })).toBeDisabled();
  // No reload, no navigation: the poll clears the busy state and both actions come back.
  await expect(prune.getByRole("button", { name: /prune selected \(1\)/i })).toBeEnabled({
    timeout: 15_000,
  });
  await expect(
    missions.getByRole("button", { name: /archive old missions \(9\)/i }),
  ).toBeEnabled({ timeout: 15_000 });
});

test("a category that could not be measured is never submitted as zero (#993)", async ({
  page,
}) => {
  let prunedWith: unknown = null;
  await maintenanceMocks(page, {
    pruneGet: (route) =>
      route.fulfill({
        json: {
          categories: {
            stale_sockets: { items: 0, bytes: 0, error: "OSError" },
            archived_scrollback: { items: 312, bytes: 188743680 },
          },
          runner: null,
        },
      }),
    prunePost: async (route) => {
      prunedWith = route.request().postDataJSON();
      await route.fulfill({
        json: {
          removed: 312,
          bytes_freed: 188743680,
          skipped: [],
          failed: [],
          failed_total: 0,
        },
      });
    },
  });
  await page.goto(settingsPath("maintenance"));
  const prune = card(page, "Prune");
  await expect(page.getByTestId("prune-count-stale_sockets")).toHaveText("couldn’t measure");
  await expect(prune.getByRole("button", { name: /prune selected/i })).toBeDisabled();
  await expect(prune.getByText(/couldn’t be measured.*deselect it or refresh/i)).toBeVisible();

  await prune.getByRole("checkbox", { name: /stale terminal sockets/i }).click();
  await prune.getByRole("checkbox", { name: /archived sessions’ scrollback/i }).click();
  await prune.getByRole("button", { name: /prune selected \(1\)/i }).click();
  await prune.getByRole("button", { name: /confirm prune/i }).click();
  await expect(page.getByText(/removed 312 items/i)).toBeVisible();
  expect(prunedWith).toEqual({ categories: ["archived_scrollback"] });
});
