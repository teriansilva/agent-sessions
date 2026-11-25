/** The session limit's way out (#1819), in a real browser on desktop and mobile: a terminal launch
 *  refused with 4429 shows why and how to get out, the list scrolls with its actions in reach, and
 *  archiving frees a slot and retries the same connection. Mocked API and socket. */
import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { settingsPath } from "../src/routes/settingsTabs";
import type { RunningSessionRow, SessionCapacity } from "../src/types/resources";
import { TOUCH_FLOOR } from "./touchTarget";

const SESSION = "11111111-1111-1111-1111-111111111111";
const REASON =
  "Session limit reached: 28 of 28 running. Archive a session or raise the limit to start another. Nothing was started.";

const row = (n: number, over: Partial<RunningSessionRow> = {}): RunningSessionRow => ({
  id: `claude:${String(n).padStart(8, "0")}-0000-0000-0000-000000000000`,
  title: `Session number ${n}`,
  engine: "claude",
  project: "agent-sessions",
  last_mtime: 1_700_000_000 + n,
  working: false,
  mission: null,
  ...over,
});
const capacity = (over: Partial<SessionCapacity> = {}): SessionCapacity => {
  const sessions = over.sessions ?? [row(1), row(2), row(3)];
  return {
    limit: 28,
    recommended: 28,
    source: "recommended",
    readable: true,
    running: 28,
    others: 0,
    sessions,
    ...over,
  };
};

/** Sockets the pane opens: refused while `refuse()` says so, accepted and quiet after. */
async function setup(page: Page, read: () => SessionCapacity | null, refuse: () => boolean) {
  await commonMocks(page);
  const sockets: WebSocketRoute[] = [];
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    sockets.push(ws);
    if (refuse()) void ws.close({ code: 4429, reason: REASON });
  });
  await page.route("**/api/sessions/capacity", (route) => {
    const body = read();
    return body
      ? route.fulfill({ json: body })
      : route.fulfill({ status: 500, json: { detail: "no" } });
  });
  return sockets;
}

async function openDialog(page: Page) {
  await page.goto(`/s/claude/${SESSION}`);
  const panel = page.locator('[data-session-opening="session-limit"]');
  await expect(panel).toContainText("28 of 28 running");
  await page.getByTestId("session-limit-free").click();
  const dialog = page.getByTestId("session-limit-dialog");
  await expect(dialog).toBeVisible();
  return dialog;
}

test("a refused launch never retries by itself; archiving frees a slot and Try again reconnects", async ({
  page,
}) => {
  let data = capacity();
  let full = true;
  const sockets = await setup(
    page,
    () => data,
    () => full,
  );
  const dialog = await openDialog(page);
  await expect(page.getByTestId("session-limit-count")).toHaveText("28 of 28 running");
  await page.waitForTimeout(1500); // 4409 and 4502 would have retried by now
  expect(sockets).toHaveLength(1);
  const retry = dialog.getByRole("button", { name: "Try again" });
  await expect(retry).toBeDisabled();

  const archived: string[] = [];
  await page.route("**/api/sessions/*/archive", (route) => {
    archived.push(decodeURIComponent(route.request().url().split("/").at(-2)!));
    data = capacity({ running: 27, sessions: [row(2), row(3)] });
    full = false;
    return route.fulfill({ json: { id: row(1).id, archived: true } });
  });
  await dialog.getByRole("listitem").first().getByRole("button", { name: "Archive" }).click();
  await expect(page.getByTestId("session-limit-count")).toHaveText("27 of 28 running");
  await expect(dialog).toContainText("“Session number 1” is archived and its history kept.");
  expect(archived).toEqual([row(1).id]);

  await retry.click();
  await expect(dialog).toBeHidden();
  await expect.poll(() => sockets.length).toBe(2);
  await expect(page.locator('[data-session-opening="session-limit"]')).toBeHidden();
});

test("the full list scrolls and its actions stay on screen", async ({ page }) => {
  const sessions = Array.from({ length: 200 }, (_, n) => row(n));
  await setup(
    page,
    () => capacity({ sessions }),
    () => true,
  );
  const dialog = await openDialog(page);
  const list = page.getByTestId("session-limit-list");
  await expect(list.getByRole("listitem")).toHaveCount(200);
  const scrolls = await list.evaluate((el) => el.scrollHeight > el.clientHeight);
  expect(scrolls).toBe(true);

  const viewport = page.viewportSize()!;
  for (const action of [
    dialog.getByRole("link", { name: "Raise the limit" }),
    dialog.getByRole("button", { name: "Try again" }),
  ]) {
    await expect(action).toBeInViewport({ ratio: 1 });
    const box = (await action.boundingBox())!;
    expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  const archive = list.getByRole("button", { name: "Archive" }).first();
  expect((await archive.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // No row is clipped sideways out of a phone's width.
  const dialogBox = (await dialog.boundingBox())!;
  expect(dialogBox.x).toBeGreaterThanOrEqual(0);
  expect(dialogBox.x + dialogBox.width).toBeLessThanOrEqual(viewport.width);
});

test("when every session is a mission's, nothing offers Archive and raising the limit leads", async ({
  page,
}) => {
  const mission = { id: "m1", title: "Migrate billing", state: "running" };
  await setup(
    page,
    () =>
      capacity({
        limit: 2,
        running: 2,
        sessions: [row(1, { mission }), row(2, { mission, working: true })],
      }),
    () => true,
  );
  const dialog = await openDialog(page);
  await expect(dialog.getByText("Held by mission")).toHaveCount(2);
  await expect(dialog.getByRole("button", { name: "Archive" })).toHaveCount(0);
  await expect(dialog).toContainText("Finish or stop the mission, or raise the limit.");
  await dialog.getByRole("link", { name: "Raise the limit" }).click();
  await expect(page).toHaveURL(new RegExp(`${settingsPath("resources")}$`));
  await expect(dialog).toBeHidden();
});

test("already over the limit after an update: it says why, and recovery needs no archive", async ({
  page,
}) => {
  await setup(
    page,
    () => capacity({ running: 31, others: 3 }),
    () => true,
  );
  const dialog = await openDialog(page);
  await expect(page.getByTestId("session-limit-count")).toHaveText("31 of 28 running");
  await expect(dialog).toContainText("set from what this machine is recommended");
  await expect(dialog).toContainText("Nothing was stopped.");
  await expect(dialog).toContainText("archive 4 sessions");
  await expect(page.getByTestId("session-limit-others")).toContainText("3 more aren't listed");
  await expect(dialog.getByRole("link", { name: "Raise the limit" })).toBeVisible();
});

test("with nothing listable it says so, and never blames a mission", async ({ page }) => {
  // A fresh terminal session has no row until its first turn: easy to reach at a limit of 1 or 2.
  await setup(
    page,
    () => capacity({ limit: 1, running: 1, others: 1, sessions: [] }),
    () => true,
  );
  const dialog = await openDialog(page);
  await expect(dialog).toContainText("None of the running sessions can be listed here.");
  await expect(dialog).not.toContainText("belongs to a mission");
  await expect(page.getByTestId("session-limit-others")).toContainText("just started");
  await expect(dialog.getByRole("link", { name: "Raise the limit" })).toBeVisible();
});

test("an unreadable list says so and can be reloaded", async ({ page }) => {
  let readable = false;
  await setup(
    page,
    () => (readable ? capacity() : null),
    () => true,
  );
  const dialog = await openDialog(page);
  await expect(dialog.getByRole("alert")).toContainText("Couldn't read the running sessions");
  readable = true;
  await dialog.getByRole("button", { name: "Reload" }).click();
  await expect(page.getByTestId("session-limit-count")).toHaveText("28 of 28 running");
  await expect(dialog.getByRole("listitem")).toHaveCount(3);
});

test("the dialog isolates the app behind it, and closing it returns focus to what opened it", async ({
  page,
}) => {
  await setup(
    page,
    () => capacity(),
    () => true,
  );
  const dialog = await openDialog(page);
  await expect(page.locator("#root")).toHaveAttribute("inert", "");
  await expect(dialog).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.locator("#root")).not.toHaveAttribute("inert", "");
  await expect(page.locator('[data-session-opening="session-limit"]')).toBeVisible();
  await expect(page.getByTestId("session-limit-free")).toBeFocused();
});

test("first-time setup explains the limit it picked for this machine, and an edit is saved", async ({
  page,
}) => {
  const saved: unknown[] = [];
  const settings = {
    values: { console_tasks: 4096, api_tasks: 4096, library_threads: 8, api_memory_gib: 8, max_sessions: 28 },
    recommended: { console_tasks: 4096, api_tasks: 4096, library_threads: 8, api_memory_gib: 8, max_sessions: 28 },
    bounds: {
      console_tasks: { min: 256, max: 16384 },
      api_tasks: { min: 256, max: 16384 },
      library_threads: { min: 1, max: 64 },
      api_memory_gib: { min: 1, max: 1024 },
      max_sessions: { min: 1, max: 256 },
    },
    sources: {
      console_tasks: "recommended",
      api_tasks: "recommended",
      library_threads: "recommended",
      api_memory_gib: "recommended",
      max_sessions: "recommended",
    },
    console_tasks_max: "4096",
    library_overrides: [],
    notice: null,
    capacity: {
      memory_bytes: 32 * 1024 ** 3,
      cpus: 8,
      reserved_bytes: 3.2 * 1024 ** 3,
      by_memory: 28,
      by_cpu: 48,
      limited_by: "memory",
      recommended: 28,
    },
    running: 0,
  };
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        onboarded: false,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) =>
    r.fulfill({
      json: { engines: [{ id: "claude", present: true, supports_new: true, bin: "/x/claude" }] },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/fs\/dirs(\?.*)?$/, (r) =>
    r.fulfill({ json: { path: "/home/u", home: "/home/u", dirs: [] } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/system/resources*", (r) => {
    if (r.request().method() === "POST") {
      saved.push(r.request().postDataJSON());
      return r.fulfill({ json: { settings } });
    }
    return r.fulfill({ json: { settings } });
  });

  await page.goto("/");
  const dialog = page.getByRole("dialog").first();
  await dialog.getByRole("button", { name: /get started/i }).click();
  await dialog.getByRole("button", { name: /skip.*continue/i }).click();
  await expect(dialog.getByText("claude", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: /^next$/i }).click();

  await expect(dialog.getByRole("heading", { name: "Sized for this machine." })).toBeVisible();
  const facts = dialog.getByTestId("session-capacity-facts");
  await expect(facts).toContainText("8");
  await expect(facts).toContainText("32 GiB");
  await expect(facts).toContainText("28");
  await expect(dialog.getByTestId("setup-system-reason")).toContainText(
    "Memory sets this limit: 1 GiB per session, after keeping 3 GiB for the system.",
  );
  await expect(dialog).toContainText("Subagents inside a session don't count.");

  // The step fits a phone: Next is reachable, and the field is a real touch target.
  const limit = dialog.getByTestId("setup-system-limit");
  expect((await limit.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  const next = dialog.getByRole("button", { name: /^next$/i });
  await next.scrollIntoViewIfNeeded();
  await expect(next).toBeInViewport({ ratio: 1 });

  await limit.fill("12");
  await next.click();
  await expect(dialog.getByRole("heading", { name: "Set up your AI" })).toBeVisible();
  expect(saved).toEqual([{ max_sessions: 12 }]);
});
