import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";

// #1189: the new-session form's Model select, in a real browser on the desktop AND mobile
// projects. The backend is mocked; the terminal socket is intercepted so the spec asserts the
// `/ws/term` URL the client ACTUALLY opens — which is the only place a model reaches the server —
// rather than a DOM proxy for it.

async function mockBackend(page: Page, engines: string[]) {
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
  await mockRoster(page);
}

/** Every `/ws/term` URL the page opens, in order. The socket is accepted and left quiet. */
async function captureSockets(page: Page): Promise<URL[]> {
  const urls: URL[] = [];
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    urls.push(new URL(ws.url()));
  });
  return urls;
}

test("default is selected and launches with no model; a chosen model rides the socket (#1189)", async ({
  page,
}) => {
  await mockBackend(page, ["claude"]);
  const sockets = await captureSockets(page);
  await page.goto("/");
  const model = page.getByRole("combobox", { name: "Model" });
  await expect(model).toBeVisible();
  await expect(model).toHaveValue("default");

  // The control fits the viewport on both projects (no horizontal overflow on the phone).
  const box = (await model.boundingBox())!;
  const vw = page.viewportSize()!.width;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(vw);

  await model.selectOption("claude-opus-5");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(/\/s\/claude\/[0-9a-f-]{36}$/);
  await expect.poll(() => sockets.length).toBeGreaterThan(0);
  const q = sockets[0].searchParams;
  expect(q.get("new")).toBe("1");
  expect(q.get("model")).toBe("claude-opus-5");
  expect(q.getAll("model")).toHaveLength(1);
});

test("a default launch sends no model parameter at all (#1189)", async ({ page }) => {
  await mockBackend(page, ["claude"]);
  const sockets = await captureSockets(page);
  await page.goto("/");
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue("default");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect.poll(() => sockets.length).toBeGreaterThan(0);
  expect(sockets[0].searchParams.has("model")).toBe(false);
});

test("switching engine clears the model, and a configured-elsewhere engine offers none (#1189)", async ({
  page,
}) => {
  await mockBackend(page, ["claude", "codex", "opencode"]);
  const sockets = await captureSockets(page);
  await page.goto("/");
  const agent = page.getByRole("combobox", { name: "Agent" });
  const model = page.getByRole("combobox", { name: "Model" });
  await model.selectOption("claude-sonnet-5");
  await agent.selectOption("codex");
  await expect(model).toHaveValue("default");
  // codex's own list, not claude's.
  await expect(model.locator("option", { hasText: "gpt-5-codex" })).toHaveCount(1);
  await expect(model.locator("option", { hasText: "claude-sonnet-5" })).toHaveCount(0);

  await agent.selectOption("opencode");
  await expect(model).toHaveCount(0);
  await expect(page.getByTestId("new-session-model-elsewhere")).toBeVisible();
  await page.getByRole("button", { name: /start session/i }).click();
  await expect.poll(() => sockets.length).toBeGreaterThan(0);
  expect(sockets[0].searchParams.has("model")).toBe(false);
});
