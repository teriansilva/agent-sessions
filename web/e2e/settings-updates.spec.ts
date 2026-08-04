import { expect, test, type Page } from "@playwright/test";

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
  await page.goto("/settings/system");
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

  test("System tab has no horizontal scroll with the grown Updates card", async ({
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
