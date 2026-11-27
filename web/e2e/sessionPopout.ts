// The pop-out spec's mocked backend and helpers (`session-popout.spec.ts`).
import { expect, type Page, type WebSocketRoute } from "@playwright/test";
import { mockApiPanes, STRUCTURED_ID } from "./apiLookFixture";

export const API = "codex-api";
export const API_KEY = `${API}:${STRUCTURED_ID}`;
export const API_TITLE = "Washed-out light-mode diff";
export const PTY = "claude";
export const PTY_ID = "aaaaaaaa-1111-2222-3333-444444444444";
export const PTY_KEY = `${PTY}:${PTY_ID}`;
export const PTY_TITLE = "Fix the auth token refresh race";
const CWD = "/home/u/proj";

function row(engine: string, uuid: string, title: string) {
  return {
    id: `${engine}:${uuid}`,
    engine,
    uuid,
    short_uuid: uuid.slice(0, 8),
    cwd: CWD,
    project: { kind: "folder", id: CWD, name: "proj" },
    last_mtime: 1_790_000_000,
    created_at: 1_789_999_000,
    first_user_message: "",
    title,
    sticky: false,
    archived: false,
    working: false,
    has_draft: false,
  };
}
const ROWS = [row(API, STRUCTURED_ID, API_TITLE), row(PTY, PTY_ID, PTY_TITLE)];

/** The app's mocked backend, on the whole context so the pop-out page sees it too. Returns the
 *  terminal socket URLs every page opened. */
export async function setup(page: Page): Promise<{ sockets: string[] }> {
  const ctx = page.context();
  // The fixture's mocks on the CONTEXT, not the page: a pop-out is a new page of the same context
  // and must see the same backend from its first request. `mockApiPanes` only calls `route` (and
  // `addInitScript`), which a context has with the same signatures.
  await mockApiPanes(ctx as unknown as Page);
  // Registered after the fixture's catch-alls, so these win.
  await ctx.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        sessions: ROWS,
        next_offset: null,
        total: ROWS.length,
        facets: { projects: [ROWS[0].project], engines: [API, PTY] },
      },
    }),
  );
  await ctx.route(/\/api\/sessions\/([a-z-]+)(?::|%3A)([0-9a-f-]+)$/, (r) => {
    const m = /\/api\/sessions\/([a-z-]+)(?::|%3A)([0-9a-f-]+)$/.exec(r.request().url());
    const found = ROWS.find((x) => m && x.engine === m[1] && x.uuid === m[2]);
    return found
      ? r.fulfill({ json: found })
      : r.fulfill({ status: 404, json: { detail: "unknown session" } });
  });
  const sockets: string[] = [];
  await ctx.routeWebSocket(/\/ws\/term\//, (ws: WebSocketRoute) => {
    sockets.push(decodeURIComponent(ws.url()));
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "ready" }));
    ws.send(Buffer.from("\x1b[1m● claude\x1b[0m  refresh race: single-flight lock added\r\n> "));
  });
  return { sockets };
}

/** The pane's own session menu (the "…" at full width, "Actions" at the compact width). */
export async function openPaneMenu(page: Page) {
  await page
    .getByRole("button", { name: /^(More session actions|Actions for this session)$/ })
    .first()
    .click();
  const menu = page.getByRole("menu").last();
  await expect(menu).toBeVisible();
  return menu;
}

/** The shell around a session: none of it may exist in a pop-out. */
export async function expectNoShell(popup: Page) {
  await expect(popup.locator(".hud-topbar")).toHaveCount(0);
  await expect(popup.locator("aside.sidebar")).toHaveCount(0);
  await expect(popup.locator("footer.hud-classbar")).toHaveCount(0);
  await expect(popup.getByRole("navigation")).toHaveCount(0);
}

/** The pane fills the window (no floating-panel margins). */
export async function expectFillsWindow(popup: Page) {
  const vp = popup.viewportSize()!;
  const box = await popup.locator("main").boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeLessThanOrEqual(1);
  expect(box!.y).toBeLessThanOrEqual(1);
  expect(box!.width).toBeGreaterThanOrEqual(vp.width - 1);
  expect(box!.height).toBeGreaterThanOrEqual(vp.height - 1);
}
