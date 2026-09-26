import { expect, type Page, test } from "@playwright/test";
import { ROSTER } from "./roster";

/** #853 P4 (#1128): the sidebar row and the map chip take an agent's badge and colour from the
 *  roster — not from a client-side list. The proof is an engine that exists ONLY in the roster:
 *  `zeta`, badged `zt` and accented magenta. The fallback for an id the roster does not list is
 *  the id's first two letters (`ze`) in slate, so a surface that still guessed would render `ze`
 *  and fail both assertions. */

const now = Math.floor(Date.now() / 1000);

const claude = ROSTER.engines.find((e) => e.id === "claude")!;
const ZETA = {
  ...claude,
  id: "zeta",
  label: "Zeta Agent",
  display: {
    ...(claude.display as Record<string, unknown>),
    badge: "zt",
    accent: "magenta",
    name: "zeta",
    order: 999,
  },
};

const sessions = [
  {
    id: "zeta:z1",
    engine: "zeta",
    uuid: "z1",
    short_uuid: "z1",
    cwd: "/home/u/proj",
    project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
    last_mtime: now - 30,
    first_user_message: "",
    title: "Zeta session",
    sticky: false,
    archived: false,
    ai_summary: "",
  },
];

async function mockApp(page: Page, withZeta: boolean) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: {
        projects: [
          {
            id: "p1",
            name: "proj",
            color: "#ffb000",
            archived: false,
            default_folder: "/home/u/proj",
          },
        ],
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) =>
    r.fulfill({
      json: {
        engines: withZeta ? [...ROSTER.engines, ZETA] : ROSTER.engines,
        problems: [],
      },
    }),
  );
  await page.route(/\/api\/sessions\/[^?]+$/, (r) =>
    r.fulfill({ status: 404, json: { detail: "not found" } }),
  );
  await page.route("**/api/sessions?**", (r) =>
    r.fulfill({
      json: {
        sessions,
        next_offset: null,
        total: sessions.length,
        facets: { projects: [], engines: ["zeta"] },
      },
    }),
  );
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: ["project:p1"],
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
      },
    }),
  );
}

/** The chip's `--eng`, and the token it should equal — both as the browser resolves them. */
async function engAndToken(page: Page, token: string) {
  return mapChip(page).evaluate((el, t) => {
    const root = getComputedStyle(document.documentElement).getPropertyValue(t).trim();
    return { eng: getComputedStyle(el).getPropertyValue("--eng").trim(), token: root };
  }, token);
}

const sidebarRow = (page: Page) => page.locator('a[href="/s/zeta/z1"]').first();
const mapChip = (page: Page) =>
  page.locator(".tr-overview .tr-ov-chip").filter({ hasText: "Zeta session" });

test("the sidebar row and the map chip take a roster-only agent's badge and colour", async ({
  page,
}, testInfo) => {
  await mockApp(page, true);
  await page.goto("/overview");

  const chip = mapChip(page);
  await expect(chip).toBeVisible();
  await expect(chip.locator(".tr-ov-eng")).toHaveText("zt");
  // The accent is a token reference, never a hex (#853 P4) — and the manifest's, not slate.
  const magenta = await engAndToken(page, "--engine-magenta");
  expect(magenta.token).not.toBe("");
  expect(magenta.eng).toBe(magenta.token);

  // The sidebar is the desktop shell's; on a phone it lives in the drawer, so only the map chip
  // is asserted there.
  if (testInfo.project.name !== "mobile") await expect(sidebarRow(page)).toContainText("zt");
});

test("negative control: the same rows without the roster entry render the fallback", async ({
  page,
}) => {
  await mockApp(page, false);
  await page.goto("/overview");
  const chip = mapChip(page);
  await expect(chip).toBeVisible();
  await expect(chip.locator(".tr-ov-eng")).toHaveText("ze");
  const slate = await engAndToken(page, "--engine-slate");
  expect(slate.token).not.toBe("");
  expect(slate.eng).toBe(slate.token);
});
