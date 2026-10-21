import { expect, type Page, test } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";
import { NEW_SESSION_PATH } from "../src/lib/routes";

// "Rename sessions with the reviewed title" (#1510) — real-browser proof. The server resolves the
// title (`metadata.display_title`); this stands in for it: with the option off, the row's title is
// the session's own first message instead of the review's AI title. Turning the option off in
// Settings → AI → Session review must re-read the open sidebar at once — no reload, no 15 s poll.
// Network fully mocked.

const NOW = Math.floor(Date.now() / 1000);
const AI_TITLE = "Fix CI runner fork-EAGAIN limits";
const OWN_TITLE = "fix the runner";

function sessions(renames: boolean) {
  return {
    sessions: [
      {
        id: "claude:aaaaaaaa-0000-0000-0000-000000000001",
        engine: "claude",
        uuid: "aaaaaaaa-0000-0000-0000-000000000001",
        short_uuid: "aaaaaaaa",
        cwd: "/home/u/infra",
        project: { kind: "folder", id: "/home/u/infra", name: "/home/u/infra" },
        last_mtime: NOW,
        first_user_message: OWN_TITLE,
        title: renames ? AI_TITLE : OWN_TITLE,
        sticky: false,
        archived: false,
        ai_summary: "Editing systemd limits; tests rerunning after thread cap",
        ai_title: AI_TITLE,
        intervention_required: false,
        intervention_reason: "",
        reviewed_at: NOW,
        review_excluded: false,
      },
    ],
    next_offset: null,
    total: 1,
    facets: { projects: [], engines: ["claude"] },
  };
}

async function statefulServer(page: Page) {
  const block = { recognise_prompts: true, decision_context: "standard", notify: true, rename_sessions: true };
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        ai_review: {
          enabled: true,
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
    const body = (r.request().postDataJSON() ?? {}) as { session_review?: Partial<typeof block> };
    Object.assign(block, body.session_review ?? {});
    await r.fulfill({ json: { session_review: { ...block } } });
  });
  await page.route(/\/api\/sessions(\?.*)?$/, (r) => r.fulfill({ json: sessions(block.rename_sessions) }));
}

test("turning the option off shows each session's own title, and back on the reviewed one", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "the session sidebar is a column on desktop only");
  await statefulServer(page);
  await page.goto(NEW_SESSION_PATH);
  const sidebar = page.locator("aside.sidebar");
  await expect(sidebar.getByText(AI_TITLE)).toBeVisible();
  // To Settings and back IN-APP (no reload): what the list shows is what the server now resolves.
  const go = (path: string) =>
    page.evaluate((p) => {
      history.pushState({}, "", p);
      dispatchEvent(new PopStateEvent("popstate"));
    }, path);
  const toggle = async (on: boolean) => {
    await go(settingsPath("ai-session-review"));
    const box = page.getByRole("checkbox", { name: "Rename sessions with the reviewed title" });
    await expect(box).toBeChecked({ checked: !on });
    await box.setChecked(on);
    await expect(box).toBeChecked({ checked: on });
    await go(NEW_SESSION_PATH);
  };
  await toggle(false);
  await expect(sidebar.getByText(OWN_TITLE, { exact: true })).toBeVisible();
  await expect(sidebar.getByText(AI_TITLE)).toHaveCount(0);
  await toggle(true);
  await expect(sidebar.getByText(AI_TITLE)).toBeVisible();
});

test("the option sits in the Session review page at phone width and wraps inside it", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "phone-width layout check");
  await statefulServer(page);
  await page.goto(settingsPath("ai-session-review"));
  const label = page.locator("label").filter({ hasText: "Rename sessions with the reviewed title" });
  await expect(label).toBeVisible();
  const [box, width] = await Promise.all([
    label.boundingBox(),
    page.evaluate(() => document.documentElement.clientWidth),
  ]);
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(width);
  // The control focuses like its siblings (keyboard reach).
  const input = page.getByRole("checkbox", { name: "Rename sessions with the reviewed title" });
  await input.focus();
  await expect(input).toBeFocused();
});
