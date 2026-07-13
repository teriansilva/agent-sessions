import { expect, test } from "@playwright/test";

// #636: the plain-terminal "shell" engine. Real browser, backend mocked so the landing renders
// without a server. Runs on both the desktop and mobile Playwright projects.
//
// Two things this proves that jsdom can't:
//   1. shell is offered in the new-session picker (config.new_session_engines), and
//   2. Start navigates to a BARE-uuid `/s/shell/<uuid>` — shell is a PINNED-id engine, so it must
//      NOT get the `new-<uuid>` placeholder that reconcile engines (opencode/antigravity) use, or
//      the ws new=1 launch would mishandle it.

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/;

async function mockBackend(page: import("@playwright/test").Page, engines: string[]) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: engines,
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: "/home/u/proj",
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
}

test("shell Start opens a bare-uuid /s/shell/<uuid>, not a new- placeholder (#636)", async ({
  page,
}) => {
  await mockBackend(page, ["shell"]);
  await page.goto("/");
  await expect(page.getByLabel("Launch folder")).toHaveValue("/home/u/proj");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/shell/${UUID.source}$`));
  // The placeholder form is reserved for reconcile engines — shell must never use it.
  await expect(page).not.toHaveURL(/\/s\/shell\/new-/);
});

test("shell is selectable in the engine picker alongside an agent engine (#636)", async ({
  page,
}) => {
  await mockBackend(page, ["claude", "shell"]);
  await page.goto("/");
  // The Agent picker shows only when >1 engine is offered; shell is one of its options.
  const agent = page.getByRole("combobox", { name: "Agent" });
  await expect(agent).toBeVisible();
  await agent.selectOption("shell");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/shell/${UUID.source}$`));
});
