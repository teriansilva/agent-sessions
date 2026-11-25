import { expect, test, type WebSocketRoute } from "@playwright/test";
import { mockRoster } from "./roster";
import { launchFolder } from "./newSessionForm";
import { NEW_SESSION_PATH } from "../src/lib/routes";

// #2087: a still-open tab that launched an adopt engine under a `new-<uuid>` placeholder keeps
// that frozen id for its live socket. After the server converges it to the engine's real id, a
// reconnect — e.g. after the agent exits (`/exit`) — must resolve the placeholder forward to the
// adopted session. It must NOT keep sending `new=1`, which would launch a fresh blank agent.
//
// Real browser, network + WebSocket mocked so the whole launch → adopt → reconnect flow runs
// without a backend. The server-side backstop is pinned in tests/test_placeholder_reconcile_reload.py.
test("reconnect after the agent exits stays on the adopted session (#2087)", async ({ page }) => {
  const realId = "a02a8694-40a5-4eb7-aaef-60359c1a8695";
  const now = Math.floor(Date.now() / 1000);

  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["antigravity"],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: "/home/u/proj",
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/files/capabilities", (r) =>
    r.fulfill({ json: { ok: true, reason: "" } }),
  );
  await page.route(/\/api\/sessions(\?|$)/, (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route(/\/api\/sessions\/[^/?]+$/, (r) => {
    const path = decodeURIComponent(new URL(r.request().url()).pathname);
    if (path.endsWith(`/antigravity:${realId}`)) {
      return r.fulfill({
        json: {
          id: `antigravity:${realId}`,
          engine: "antigravity",
          uuid: realId,
          short_uuid: realId.slice(0, 8),
          cwd: "/home/u/proj",
          project: { kind: "folder", id: "/home/u/proj", name: "/home/u/proj" },
          last_mtime: now,
          created_at: now - 60,
          title: "Adopted antigravity session",
          first_user_message: "hello",
          sticky: false,
          archived: false,
        },
      });
    }
    return r.fulfill({ status: 404, json: { detail: "unknown session" } });
  });

  await mockRoster(page);

  const wsUrls: string[] = [];
  const routes: WebSocketRoute[] = [];
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    wsUrls.push(ws.url());
    routes.push(ws);
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "ready" }));
  });

  // A real fresh launch from the landing: the terminal opens the placeholder with new=1.
  await page.goto(NEW_SESSION_PATH);
  await expect(launchFolder(page)).toHaveAttribute("data-cwd", "/home/u/proj");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect.poll(() => wsUrls.length).toBe(1);

  const firstUrl = decodeURIComponent(wsUrls[0]);
  expect(firstUrl).toContain("/ws/term/antigravity:new-");
  expect(firstUrl).toContain("new=1");
  const placeholderKey = firstUrl.split("/ws/term/")[1].split("?")[0];

  // The engine mints its real id after the first message; the server converges this tab.
  routes[0].send(JSON.stringify({ t: "id", sid: `antigravity:${realId}` }));
  await expect(page).toHaveURL(new RegExp(`/s/antigravity/${realId}$`));

  // The agent exits: the master's socket closes. The tab reconnects on its frozen placeholder,
  // which the server resolves forward to the adopted id — never a fresh `new=1` launch.
  await routes[0].close({ code: 1000 });
  await expect.poll(() => wsUrls.length, { timeout: 20_000 }).toBe(2);

  const reconnectUrl = decodeURIComponent(wsUrls[1]);
  expect(reconnectUrl).toContain(`/ws/term/${placeholderKey}`);
  expect(reconnectUrl).not.toContain("new=1");
  // The operator stays on the adopted session's URL; no second session was created.
  await expect(page).toHaveURL(new RegExp(`/s/antigravity/${realId}$`));
});
