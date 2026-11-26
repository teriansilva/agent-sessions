import { expect, type Page, test } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

// "Title new sessions once" (#2332) — real-browser proof, desktop and phone. The server's defaults
// since #2332 are renaming OFF and title-once ON; the switch saves `session_review.title_once`
// alone, and a reload shows the stored value. Network fully mocked by a stateful stand-in.

async function statefulServer(page: Page) {
  const block: Record<string, unknown> = {
    recognise_prompts: true,
    decision_context: "standard",
    notify: true,
    rename_sessions: false,
    title_once: true,
  };
  const saves: unknown[] = [];
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        ai_review: {
          enabled: false,
          base_url: "https://ai.example.io/v1",
          model: "m",
          interval_minutes: 5,
          max_input_chars: 24000,
          api_key_set: true,
          configured: true,
        },
        session_review: { ...block },
      },
    }),
  );
  await page.route("**/api/prefs", async (r) => {
    const body = (r.request().postDataJSON() ?? {}) as { session_review?: object };
    saves.push(body);
    Object.assign(block, body.session_review ?? {});
    await r.fulfill({ json: { session_review: { ...block } } });
  });
  return saves;
}

test("the defaults show, the switch saves title_once alone, and a reload keeps it", async ({
  page,
}) => {
  const saves = await statefulServer(page);
  await page.goto(settingsPath("ai-session-review"));
  const rename = page.getByRole("checkbox", { name: "Rename sessions with the reviewed title" });
  const once = page.getByRole("checkbox", { name: "Title new sessions once" });
  await expect(rename).not.toBeChecked();
  await expect(once).toBeChecked();
  await once.setChecked(false);
  await expect(once).not.toBeChecked();
  expect(saves).toEqual([{ session_review: { title_once: false } }]);
  await page.reload();
  await expect(page.getByRole("checkbox", { name: "Title new sessions once" })).not.toBeChecked();
  const renameAfter = page.getByRole("checkbox", { name: "Rename sessions with the reviewed title" });
  await expect(renameAfter).not.toBeChecked();
  // Renaming on: the title follows every review, so the once-title switch stands down.
  await renameAfter.setChecked(true);
  await expect(page.getByRole("checkbox", { name: "Title new sessions once" })).toBeDisabled();
  await expect(page.getByText(/not used while renaming is on/i)).toBeVisible();
});

test("the switch sits inside the page at phone width and is reachable by keyboard", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "phone-width layout check");
  await statefulServer(page);
  await page.goto(settingsPath("ai-session-review"));
  const label = page.locator("label").filter({ hasText: "Title new sessions once" });
  await label.scrollIntoViewIfNeeded();
  await expect(label).toBeVisible();
  const [box, width] = await Promise.all([
    label.boundingBox(),
    page.evaluate(() => document.documentElement.clientWidth),
  ]);
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(width);
  const input = page.getByRole("checkbox", { name: "Title new sessions once" });
  await input.focus();
  await expect(input).toBeFocused();
});
