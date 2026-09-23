import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

// #538: real-browser proof for the in-app auto-update controls in Settings → System.
// The Automatic-updates toggle and the release-channel radiogroup persist via
// POST /api/update/settings (mocked here — the server semantics are covered by
// tests/test_api.py + tests/test_update*.py). The mobile project additionally proves
// the card reflows single-column without horizontal scroll on a phone viewport.

async function setup(page: Page): Promise<unknown[]> {
  const posts: unknown[] = [];
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
      },
    }),
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
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "1.2.3" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) =>
    r.fulfill({ json: { os: "Linux" } }),
  );
  await page.route("**/api/update/settings", (r) => {
    if (r.request().method() === "POST") {
      const body = r.request().postDataJSON() as {
        auto_update?: boolean;
        channel?: string;
      };
      posts.push(body);
      return r.fulfill({
        json: {
          auto_update: body.auto_update ?? false,
          channel: body.channel ?? "stable",
          last_auto: null,
        },
      });
    }
    return r.fulfill({
      json: { auto_update: false, channel: "stable", last_auto: null },
    });
  });
  await page.goto(settingsPath("updates"));
  return posts;
}

test.describe("in-app auto-update settings (#538)", () => {
  test("toggle + channel persist via /api/update/settings", async ({
    page,
  }) => {
    const posts = await setup(page);

    const toggle = page.getByRole("checkbox", { name: /automatic updates/i });
    await expect(toggle).toBeVisible();
    await expect(toggle).toBeEnabled(); // enabled once the persisted settings loaded
    await expect(toggle).not.toBeChecked(); // default off — the opt-in posture is preserved
    await toggle.click();
    await expect(toggle).toBeChecked();
    // With auto-update on and no pass yet this run, the recent-runtime status line shows.
    await expect(
      page.getByText(/no automatic check yet since the last restart/i),
    ).toBeVisible();

    const main = page.getByRole("radio", { name: /main/i });
    const stable = page.getByRole("radio", { name: /stable/i });
    await expect(stable).toHaveAttribute("aria-checked", "true");
    await main.click();
    await expect(main).toHaveAttribute("aria-checked", "true");
    await expect(stable).toHaveAttribute("aria-checked", "false");

    expect(posts).toEqual([{ auto_update: true }, { channel: "main" }]);
  });

  test("the Updates page has no horizontal scroll with the grown Updates card", async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name !== "mobile",
      "the single-column reflow is a mobile concern",
    );
    await setup(page);
    await expect(
      page.getByRole("checkbox", { name: /automatic updates/i }),
    ).toBeVisible();
    const overflow = await page.evaluate(
      () =>
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
});

// #1085: an update shows its progress — step, bar, elapsed — THROUGH the restart it causes, and
// What's new links the release notes on stable. The server is mocked: the progress sequence is
// what `GET /api/update/progress` returns while the installer runs, and a dropped request stands
// in for the restart window (the real server is down for a moment there).
test("Update now shows the installer's progress through the restart, and links the release notes (#1085)", async ({
  page,
}) => {
  const now = () => Math.floor(Date.now() / 1000);
  const started = now();
  let applied = false;
  let reads = 0;
  const run = (step: string, index: number, label: string) => ({
    state: "running",
    steps: 7,
    step,
    step_index: index,
    label,
    started_at: started,
    elapsed_s: now() - started,
    last_duration_s: 360,
  });
  await page.route("**/api/update/check", (r) =>
    r.fulfill({
      json: { current: "1.2.3", channel: "stable", latest: "v1.3.0", update_available: true },
    }),
  );
  await page.route("**/api/update/apply", (r) => {
    applied = true;
    return r.fulfill({ status: 202, json: { status: "updating" } });
  });
  await page.route("**/api/update/progress", (r) => {
    if (!applied) return r.fulfill({ json: { state: "idle", steps: 7, last_duration_s: 360 } });
    reads += 1;
    if (reads === 1) return r.fulfill({ json: run("web", 4, "Building the web UI") });
    if (reads === 2) return r.fulfill({ json: run("restart", 6, "Restarting the service") });
    if (reads === 3) return r.abort("connectionrefused");
    return r.fulfill({
      json: {
        ...run("health", 7, "Checking it came back"),
        state: "done",
        elapsed_s: now() - started + 1,
      },
    });
  });
  await setup(page);

  // Before anything runs: the last duration, and What's new with the installed release's notes.
  await expect(page.getByTestId("update-last-duration")).toHaveText("The last update took 6 min.");
  const whatsNew = page.getByTestId("update-whats-new");
  await expect(whatsNew).toBeVisible();
  await expect(page.getByTestId("update-release-notes")).toHaveAttribute(
    "href",
    "https://github.com/teriansilva/agent-sessions/releases/tag/v1.2.3",
  );

  await page.getByRole("button", { name: /check for updates/i }).click();
  await expect(page.getByText("Update available: v1.3.0")).toBeVisible();
  // …and now the notes link is the release on offer.
  await expect(page.getByTestId("update-release-notes")).toHaveAttribute(
    "href",
    "https://github.com/teriansilva/agent-sessions/releases/tag/v1.3.0",
  );

  await page.getByTestId("update-apply").click();
  const panel = page.getByTestId("update-progress");
  await expect(panel).toContainText("Step 4 of 7 · Building the web UI");
  await expect(panel).toContainText("last update took 6 min");
  await expect(page.getByRole("progressbar", { name: "Update progress" })).toHaveAttribute(
    "aria-valuenow",
    "4",
  );
  await expect(panel).toContainText("Step 6 of 7 · Restarting the service");
  // The dropped read is the restart, not an error.
  await expect(panel).toContainText("Restarting… reconnecting");
  await expect(panel).toContainText("Update finished");
  await expect(page.getByTestId("update-reload")).toBeVisible();
  // Terminal: the panel stops asking.
  const settled = reads;
  await page.waitForTimeout(4_500);
  expect(reads).toBe(settled);
  // No sideways scroll on a phone.
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    page.viewportSize()!.width,
  );
});
