import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";
import { NEW_SESSION_PATH } from "../src/lib/routes";

/** An old `/s/<source>/<native-id>` link opens the API session that owns that history (#1405).
 *
 * An API session (`codex-api`, `claude-api`, `opencode-api`) has its own BattleLab id; the vendor
 * history behind it has a different native id, hidden from the console engine so one
 * conversation is not listed twice. Links to the native id were posted anyway (agents' session
 * records), and opening one 404'd its lookup and left the pane on a console terminal that could
 * never attach. The server now answers that lookup with the OWNER's row, marked `resolved_from`;
 * the real SessionsProvider accepts only that declared alias, and SessionView replaces the URL.
 *
 * Mocks are exact-path: the lookup for the native key and the owner key are separate routes, so
 * the alias contract is what is exercised, not a broad pattern handing back the same object.
 * Against `main` every case stays on `/s/<source>/<native>` with a terminal and no API pane. */

const PAIRS = [
  { source: "codex", api: "codex-api", native: "01a1265f-814b-7301-ab6f-8aaefc65672a" },
  { source: "claude", api: "claude-api", native: "24c5e209-0000-4000-8000-000000000001" },
  { source: "opencode", api: "opencode-api", native: "ses_ee02dcd9DeepLinkFixture" },
] as const;
const APP = "6c51dd58-9155-47b0-aafa-2ccf0de04908";
const CWD = "/home/u/proj";
const NOW = Math.floor(Date.now() / 1000);

/** `/api/sessions/<engine>:<id>` exactly — the colon may arrive encoded. */
function lookupPath(engine: string, id: string): RegExp {
  return new RegExp(`/api/sessions/${engine}(?::|%3A)${id}$`);
}

function ownerRow(api: string) {
  return {
    id: `${api}:${APP}`,
    engine: api,
    uuid: APP,
    short_uuid: APP.slice(0, 8),
    cwd: CWD,
    project: { kind: "folder", id: CWD, name: CWD },
    last_mtime: NOW - 600,
    created_at: NOW - 900,
    title: "the owning API session",
    first_user_message: "hello",
    sticky: false,
    archived: false,
  };
}

async function setup(page: Page, pair: (typeof PAIRS)[number]) {
  const { source, api, native } = pair;
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [source, api],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: CWD,
        overview_expanded: [],
        projects_hidden: [],
      },
    }),
  );
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await mockRoster(page, {
    overrides: { [api]: { present: true, supports_new: true } },
  });
  // The native key's lookup answers with the OWNER, declared as an alias of the key asked for.
  await page.route(lookupPath(source, native), (r) =>
    r.fulfill({ json: { ...ownerRow(api), resolved_from: `${source}:${native}` } }),
  );
  // The owner's own lookup, as the API pane asks for it after the replace.
  await page.route(lookupPath(api, APP), (r) => r.fulfill({ json: ownerRow(api) }));
  // The API session's structured snapshot, so its pane renders.
  await page.route(new RegExp(`/api/structured/sessions/${api}(?::|%3A)${APP}(/[a-z]+)?(\\?.*)?$`), (r) => {
    const snap = {
      session_key: `${api}:${APP}`,
      revision: 1,
      event_cursor: 1,
      cwd: CWD,
      state: "idle",
      active_turn: null,
      model_requested: null,
      model_effective: null,
      turns: [],
      omitted_turns: 0,
      pending_requests: [],
      native: { native_id: native, worker: "w1", background_active: false },
      read_only: null,
    };
    if (r.request().url().includes("/events")) {
      return r.fulfill({ json: { session_key: snap.session_key, revision: 1, next_cursor: 1, events: [] } });
    }
    return r.fulfill({ json: snap });
  });
}

for (const pair of PAIRS) {
  test(`/s/${pair.source}/<native> opens the owning ${pair.api} session, replacing the URL`, async ({ page }) => {
    await setup(page, pair);
    // A first page, so "back" has somewhere to go that is not the native URL.
    await page.goto(NEW_SESSION_PATH);
    await expect(page.locator("#root")).toBeVisible();
    await page.goto(`/s/${pair.source}/${pair.native}`);

    await expect(page).toHaveURL(new RegExp(`/s/${pair.api}/${APP}$`));
    await expect(page.getByTestId("structured-pane")).toBeVisible();
    // The terminal mounted for the native key is gone — the API session has no terminal.
    await expect(page.locator(".xterm")).toHaveCount(0);

    // Replaced, not pushed: back leaves the session; it does not return to the native link.
    await page.goBack();
    await expect(page).not.toHaveURL(new RegExp(`/s/${pair.source}/`));
    await expect(page).not.toHaveURL(new RegExp(`/s/${pair.api}/`));
  });
}

test("a lookup without a declared alias leaves the URL alone (control)", async ({ page }) => {
  const pair = PAIRS[0];
  await setup(page, pair);
  // An answer that is someone else's row, NOT declared as an alias: the identity guard refuses
  // it, so the page must not move.
  await page.route(lookupPath(pair.source, pair.native), (r) => r.fulfill({ json: ownerRow(pair.api) }));
  await page.goto(`/s/${pair.source}/${pair.native}`);
  await expect(page.locator("#root")).toBeVisible();
  // Give the lookup (and its retry) time to land; the URL must still be the native one.
  await page.waitForResponse(lookupPath(pair.source, pair.native));
  await page.waitForTimeout(1_500);
  await expect(page).toHaveURL(new RegExp(`/s/${pair.source}/${pair.native}$`));
  await expect(page.getByTestId("structured-pane")).toHaveCount(0);
});
