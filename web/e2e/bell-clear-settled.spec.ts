/** Clear hides WHAT WAS ON SCREEN, never "the window as it stands now" (#862 / #878).
 *
 * The race this closes: a decision settles between the render and the click. Clearing "the
 * current settled window" would then hide a row the operator never saw — the second dismissal,
 * in a second place, that this feature exists to remove.
 *
 * This has to be a REAL BROWSER test, and that is the whole reason the file exists. The unit
 * test for it mocks `api.clearSettledNotifications`, so it asserts the component's argument and
 * can say nothing about the click → API adapter → POST body path. A regression anywhere in that
 * adapter — a renamed field, a dropped array, a body built from state instead of the argument —
 * passes the mocked test and ships. Here the assertion is on the bytes that actually go out.
 */
import { expect, test, type Page } from "@playwright/test";

import { mockMissions } from "./mission-console";

const NOW = Math.floor(Date.now() / 1000);

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  pulse: { configured: true },
};

function settled(id: string, title: string) {
  return {
    id,
    title,
    reason: "decided",
    project: "agent-sessions",
    engine: "claude",
    session_id: "claude:abc",
    action_id: `act-${id}`,
    ts: NOW - 60,
    read: true,
    escalation: true,
  };
}

/** The row the operator SEES, and the one that arrives after the render. */
const SHOWN = settled("n-shown", "decided while you were looking");
const LATE = settled("n-late", "settled after the render");

async function mockApp(
  page: Page,
  state: { rows: unknown[] },
  posts: string[],
) {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "t" } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 2,
        generated_at: NOW,
        window_days: 3,
        scan_depth: "fast",
        input_fingerprint: null,
        synthesis_skipped: false,
        banner: null,
        cards: [],
      },
    }),
  );

  // Registered BEFORE the listing route so the listing (registered later) wins for the plain
  // `/notifications` path — Playwright matches most-recently-registered first.
  await page.route("**/api/pulse/notifications/clear-settled", async (r) => {
    posts.push(r.request().postData() ?? "");
    const body = JSON.parse(r.request().postData() || "{}");
    const ids: string[] = body.ids ?? [];
    // The SERVER hides exactly what it was asked to hide. A row it was not told about stays.
    state.rows = state.rows.filter(
      (n) => !ids.includes((n as { id: string }).id),
    );
    return r.fulfill({ json: { cleared: ids.length } });
  });
  await page.route(/\/api\/pulse\/notifications$/, (r) =>
    r.fulfill({
      json: {
        notifications: [],
        unread: 0,
        uncertain: 0,
        settled: state.rows,
      },
    }),
  );
}

test("Clear sends the ids that were DISPLAYED, and a row that arrived later survives", async ({
  page,
}) => {
  const posts: string[] = [];
  const state = { rows: [SHOWN] as unknown[] };
  await mockApp(page, state, posts);
  await mockMissions(page);
  await page.goto("/pulse");

  const bell = page.getByRole("button", { name: /^Notifications/ });
  await bell.click();
  await expect(page.getByTestId("bell-settled-row")).toHaveCount(1);
  await expect(page.getByTestId("bell-settled")).toContainText(
    "decided while you were looking",
  );

  // …a second decision settles server-side AFTER the render. The panel is not re-fetched, so
  // the operator has not seen it.
  state.rows = [SHOWN, LATE];

  await page.getByTestId("bell-clear-settled").click();

  // The BYTES that went out name only the row that was on screen. This is the assertion a
  // mocked unit test cannot make: it proves the adapter carried the displayed ids, not a
  // re-read of the current window.
  await expect.poll(() => posts.length).toBe(1);
  const body = JSON.parse(posts[0]) as { ids: string[] };
  expect(body.ids).toEqual(["n-shown"]);
  expect(body.ids).not.toContain("n-late");

  // …and the unseen row is still there, rather than having been swept away with a dismissal it
  // never got. No reopen needed: clearing refetches the listing, so the panel already shows what
  // the server kept.
  //
  // Closing and reopening the bell to force that refetch was the first version, and it fails on
  // MOBILE — where the panel is a portalled `aria-modal` drawer (#750) that intercepts the click
  // on the button underneath it. Two mount paths, and only one of them was exercised.
  await expect(page.getByTestId("bell-settled")).toContainText(
    "settled after the render",
  );
  await expect(page.getByTestId("bell-settled")).not.toContainText(
    "decided while you were looking",
  );
});
