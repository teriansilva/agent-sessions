import { expect, test } from "@playwright/test";
import { PROJECTS_FOLDERS_PATH } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";

// #683: the folder-visibility controls once rendered at the browser default (unbranded, uncapped).
// Since #2143 they are NIGHTJAR primitives on HQ → Projects → Folders & discovery. Network is mocked
// so the page renders without a backend.

test.beforeEach(async ({ page }) => {
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
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [{ cwd: "/home/u/test", label: "test" }] } }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) =>
    r.fulfill({
      json: { auto_update: true, current: "test", channel: "stable" },
    }),
  );
  await page.route("**/api/projects**", (r) =>
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
});

// The Session-overview mode radios (#683) became NIGHTJAR's Seg when the card moved to HQ → Projects →
// Folders & discovery (#2143): one radiogroup, the chosen segment `aria-checked`, the 44 px floor
// where a finger is the pointer, and the choice persisted as `projects_mode`.
test("the overview visibility modes are a Seg that persists projects_mode (#683, #2143)", async ({ page }, testInfo) => {
  let saved: unknown = null;
  await page.route("**/api/prefs", async (r) => {
    saved = r.request().postDataJSON();
    await r.fulfill({ json: saved });
  });
  await page.goto(PROJECTS_FOLDERS_PATH);
  const group = page.getByRole("radiogroup", { name: "Project visibility" });
  await expect(group).toHaveClass(/nj-seg/);
  const all = group.getByRole("radio", { name: /show all/i });
  const included = group.getByRole("radio", { name: /only included/i });
  await expect(all).toHaveAttribute("aria-checked", "true");
  if (testInfo.project.name === "mobile")
    for (const r of [all, included]) expect((await r.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await included.click();
  await expect.poll(() => saved).toEqual({ projects_mode: "included" });
  await expect(included).toHaveAttribute("aria-checked", "true");
});
