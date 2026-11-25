import { expect, test } from "@playwright/test";
import { mockRoster } from "./roster";
import { launchFolder } from "./newSessionForm";
import { NEW_SESSION_PATH } from "../src/lib/routes";

// Reconcile engines (#163/#315/#449): creating a session must navigate to a `new-<uuid>`
// placeholder id (which the ws new=1 launch accepts + reconciles), NOT a bare UUID (which 4404s
// "session not found"). #454: antigravity was minting a bare UUID — covered here too.
// Real browser, network mocked so the landing renders without a backend.

for (const engine of ["opencode", "antigravity"] as const) {
  test(`${engine} Start navigates to a new-<uuid> placeholder, not a bare uuid (#163/#454)`, async ({
    page,
  }) => {
    await page.route("**/api/config", (r) =>
      r.fulfill({
        json: {
          csrf: "x",
          new_session_engines: [engine],
          terminal_backend: "ws",
          auth_mode: "none",
          default_project: "/home/u/proj", // folder default when no project is selected (#448)
        },
      }),
    );
    await page.route(/\/api\/projects(\?.*)?$/, (r) =>
      r.fulfill({ json: { projects: [] } }),
    );
    await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
    await page.route("**/api/version", (r) =>
      r.fulfill({ json: { version: "test" } }),
    );

    await mockRoster(page); // the manifest-generated roster (#853 P4)
    await page.goto(NEW_SESSION_PATH);
    await expect(launchFolder(page)).toHaveAttribute("data-cwd", "/home/u/proj");
    await page.getByRole("button", { name: /start session/i }).click();
    await expect(page).toHaveURL(
      new RegExp(
        `/s/${engine}/new-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`,
      ),
    );
  });
}

test("reloading /s/antigravity/new-<uuid> updates the URL to the real session id once adopted (#2097)", async ({
  page,
}) => {
  const placeholderUuid = "ad83f61c-f868-4f59-912f-747b72c90467";
  const placeholderId = `new-${placeholderUuid}`;
  const placeholderKey = `antigravity:${placeholderId}`;
  const realId = "a02a8694-40a5-4eb7-aaef-60359c1a8695";
  const realKey = `antigravity:${realId}`;
  const now = Math.floor(Date.now() / 1000);
  const adoptedRow = {
    id: realKey,
    resolved_from: placeholderKey,
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
  };

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
        sessions: [adoptedRow],
        next_offset: null,
        total: 1,
        facets: { projects: [], engines: ["antigravity"] },
      },
    }),
  );
  await page.route(/\/api\/sessions\/[^/?]+$/, (r) => {
    const reqUrl = decodeURIComponent(new URL(r.request().url()).pathname);
    if (reqUrl.endsWith(`/${placeholderKey}`) || reqUrl.endsWith(`/${realKey}`)) {
      return r.fulfill({ json: adoptedRow });
    }
    return r.fulfill({ status: 404, json: { detail: "unknown session" } });
  });

  const wsUrls: string[] = [];
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    wsUrls.push(ws.url());
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "ready" }));
  });

  await mockRoster(page);
  // Direct navigation / reload on `/s/antigravity/new-<uuid>` has no `location.state.fresh`.
  await page.goto(`/s/antigravity/${placeholderId}`);
  await expect(page).toHaveURL(new RegExp(`/s/antigravity/${realId}$`));
  // And the terminal stays mounted on the single placeholder socket without reconnecting.
  await expect.poll(() => wsUrls.length).toBe(1);
  expect(decodeURIComponent(wsUrls[0])).toContain(`/ws/term/${placeholderKey}`);
  expect(wsUrls[0]).not.toContain("new=1");
});

test("reloading /s/antigravity/new-<uuid> before first turn attaches and converges when server broadcasts id (#2097)", async ({
  page,
}) => {
  const placeholderUuid = "ad83f61c-f868-4f59-912f-747b72c90467";
  const placeholderId = `new-${placeholderUuid}`;
  const placeholderKey = `antigravity:${placeholderId}`;
  const realId = "a02a8694-40a5-4eb7-aaef-60359c1a8695";
  const realKey = `antigravity:${realId}`;

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
  await page.route(/\/api\/sessions\/[^/?]+$/, (r) =>
    r.fulfill({ status: 404, json: { detail: "unknown session" } }),
  );

  const wsUrls: string[] = [];
  let sendIdFrame: (() => void) | null = null;
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    wsUrls.push(ws.url());
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "ready" }));
    sendIdFrame = () => ws.send(JSON.stringify({ t: "id", sid: realKey }));
  });

  await mockRoster(page);
  await page.goto(`/s/antigravity/${placeholderId}`);
  await expect.poll(() => wsUrls.length).toBe(1);
  expect(decodeURIComponent(wsUrls[0])).toContain(`/ws/term/${placeholderKey}`);
  expect(wsUrls[0]).not.toContain("new=1");
  await expect(page).toHaveURL(new RegExp(`/s/antigravity/${placeholderId}$`));

  // Simulate late adoption (>5 min after launch) broadcasting `{"t":"id"}` to the attached viewer.
  sendIdFrame!();
  await expect(page).toHaveURL(new RegExp(`/s/antigravity/${realId}$`));
  expect(wsUrls).toHaveLength(1);
});
