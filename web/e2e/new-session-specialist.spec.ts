import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";
import { agentPicker } from "./newSessionForm";
import { NEW_SESSION_PATH } from "../src/lib/routes";

// #1693 S4b: "Start from a specialist" on the New session form, in a real browser on the desktop
// project and on the mobile one at the smallest supported phone width (320 px). The backend is
// mocked; the terminal socket is intercepted, so the spec asserts the `/ws/term` URL the client
// ACTUALLY opens — the only place a specialist reaches the server.

test.beforeEach(async ({ page }, testInfo) => {
  if (testInfo.project.name === "mobile") await page.setViewportSize({ width: 320, height: 780 });
});

function specialist(id: string, title: string, engine: string, model: string, live: number) {
  return {
    id,
    revision: 1,
    archived: false,
    grade: null,
    live,
    skills_missing: [],
    skills_outdated: [],
    held: [],
    current: {
      version: 3,
      title,
      summary: "",
      skills: [{ id: "tdd", revision: "r1" }],
      engine,
      model,
      effort: null,
      capacity: 2,
      created_at: 0,
    },
  };
}

async function mockBackend(page: Page) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude", "codex"],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: "/home/u/proj",
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/specialists", (r) =>
    r.fulfill({
      json: {
        specialists: [
          specialist("implementer", "Implementer", "claude", "claude-opus-5", 1),
          specialist("reviewer", "Reviewer", "codex", "default", 0),
          specialist("busy", "Busy", "claude", "default", 2),
        ],
        templates: [],
        max_instances: 6,
        live_total: 3,
      },
    }),
  );
  await mockRoster(page);
}

async function captureSockets(page: Page): Promise<URL[]> {
  const urls: URL[] = [];
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    urls.push(new URL(ws.url()));
  });
  return urls;
}

const picker = (page: Page) => page.getByRole("combobox", { name: "Specialist" });
const note = (page: Page) => page.getByTestId("new-session-specialist-note");

test("choosing a specialist preselects its agent and model and launches bound to its version", async ({
  page,
}) => {
  await mockBackend(page);
  const sockets = await captureSockets(page);
  await page.goto(NEW_SESSION_PATH);
  await expect(picker(page)).toHaveValue("");

  // The picker fits the viewport (no horizontal overflow on a 320 px phone).
  const box = (await picker(page).boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width);

  await picker(page).selectOption("reviewer");
  await expect(agentPicker(page).radio("codex")).toBeChecked();
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue("default");
  await expect(note(page)).toHaveText(
    "Reviewer v3: its skills (tdd) load into this session, read-only, with its notes for this project. Changing the agent or model below starts a plain session instead.",
  );

  await picker(page).selectOption("implementer");
  await expect(agentPicker(page).radio("claude")).toBeChecked();
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue("claude-opus-5");

  await page.getByRole("button", { name: /start session/i }).click();
  await expect.poll(() => sockets.length).toBeGreaterThan(0);
  const q = sockets[0].searchParams;
  expect(q.get("new")).toBe("1");
  expect(q.get("specialist")).toBe("implementer");
  expect(q.get("specialist_version")).toBe("3");
  expect(q.get("model")).toBe("claude-opus-5");
});

test("a specialist at capacity is listed but cannot be chosen", async ({ page }) => {
  await mockBackend(page);
  await page.goto(NEW_SESSION_PATH);
  const busy = picker(page).locator('option[value="busy"]');
  await expect(busy).toHaveText("Busy · claude · 2/2 running · at capacity");
  // `toBeDisabled` does not read an <option>'s own `disabled`: ask the DOM.
  expect(await busy.evaluate((o) => (o as HTMLOptionElement).disabled)).toBe(true);
  expect(
    await picker(page)
      .locator('option[value="implementer"]')
      .evaluate((o) => (o as HTMLOptionElement).disabled),
  ).toBe(false);
  // `selectOption` picks a disabled <option> anyway: the form itself refuses it.
  await picker(page).selectOption("busy");
  await expect(picker(page)).toHaveValue("");
  await expect(note(page)).toHaveCount(0);
});

for (const change of ["agent", "model"] as const) {
  test(`changing the ${change} after choosing a specialist starts a plain session`, async ({
    page,
  }) => {
    await mockBackend(page);
    const sockets = await captureSockets(page);
    await page.goto(NEW_SESSION_PATH);
    await picker(page).selectOption("implementer");
    await expect(note(page)).toBeVisible();

    if (change === "agent") {
      await agentPicker(page).selectOption("codex");
      await agentPicker(page).selectOption("claude"); // back again: still plain
    } else {
      await page.getByRole("combobox", { name: "Model" }).selectOption("default");
    }
    await expect(note(page)).toHaveCount(0);
    await expect(picker(page)).toHaveValue("");

    await page.getByRole("button", { name: /start session/i }).click();
    await expect.poll(() => sockets.length).toBeGreaterThan(0);
    expect(sockets[0].searchParams.has("specialist")).toBe(false);
    expect(sockets[0].searchParams.has("specialist_version")).toBe(false);
  });
}

test("with no pool the form is a plain New session", async ({ page }) => {
  await mockBackend(page);
  await page.route("**/api/specialists", (r) =>
    r.fulfill({ status: 500, json: { detail: "store unreadable" } }),
  );
  await page.goto(NEW_SESSION_PATH);
  await expect(page.getByRole("button", { name: /start session/i })).toBeVisible();
  await expect(picker(page)).toHaveCount(0);
});
