/** The mission route is `/mission`, and `/pulse` keeps working (#948 P1).
 *
 * `/pulse` is carried by bookmarks, notifications already sitting in the OS tray and push payloads
 * the service worker cached, so it is a permanent replace-redirect that keeps the query. The query
 * matters because `?m=<mission id>` is the one deep link into a specific mission: the session
 * header and row menu will use it, and it has to survive the hop.
 *
 * The linked mission is deliberately the SECOND rail row. The console auto-selects the first
 * mission when nothing is chosen, so a deep link to the first row would pass even if the link
 * were ignored entirely.
 */
import { expect, test, type Page } from "@playwright/test";
import { MISSION, missionList, missionRow, mockMissions } from "./mission-console";

const FIRST = "msn_" + "a".repeat(32);
const LINKED = "msn_" + "b".repeat(32);

async function setup(page: Page) {
  // Playwright refuses a spec that imports another spec, so the shell mocks live here rather than
  // borrowing `mission-sections.spec.ts`'s setup.
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }));
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await mockMissions(page, {
    missions: missionList([
      missionRow({ id: FIRST, title: "First mission" }),
      missionRow({ id: LINKED, title: "Deep linked mission" }),
    ]),
  });
  // Answer each mission's detail read with ITS OWN title. The header prefers the fetched title
  // over the rail row's (that is what keeps a rename correct), so one fixed detail for every id
  // would make the header name the linked mission whichever mission was actually selected.
  await page.route(/\/api\/missions\/msn_[0-9a-f]{32}(\?.*)?$/, (r) => {
    const id = /msn_[0-9a-f]{32}/.exec(r.request().url())![0];
    const title = id === LINKED ? "Deep linked mission" : "First mission";
    return r.fulfill({ json: { ...MISSION, id, title, events: [], events_next_seq: null } });
  });
}

/** Every per-mission read the page makes, by the id it named. */
function missionDetailRequests(page: Page): string[] {
  const seen: string[] = [];
  page.on("request", (req) => {
    const m = /\/api\/missions\/([^/?]+)/.exec(new URL(req.url()).pathname);
    if (m) seen.push(decodeURIComponent(m[1]));
  });
  return seen;
}

test("the Missions section lives at /mission", async ({ page }) => {
  await setup(page);
  await page.goto("/");
  const link = page.getByRole("navigation", { name: "Main sections" }).getByRole("link", { name: "Missions" });
  await expect(link).toHaveAttribute("href", "/mission");
  await link.click();
  await expect(page).toHaveURL(/\/mission$/);
  await expect(link).toHaveAttribute("aria-current", "page");
});

test("/pulse redirects to /mission and keeps ?m=, which selects that mission", async ({ page }) => {
  await setup(page);
  const details = missionDetailRequests(page);
  await page.goto(`/pulse?m=${LINKED}`);
  await expect(page).toHaveURL(/\/mission$/);
  await expect(page.getByTestId("console-title")).toHaveText("Deep linked mission");
  await expect(
    page.getByTestId("rail-mission").filter({ hasText: "Deep linked mission" }),
  ).toHaveAttribute("aria-current", "true");
  expect(details).toContain(LINKED);
});

test("a malformed ?m= selects nothing, costs no request, and the URL still settles", async ({ page }) => {
  await setup(page);
  const details = missionDetailRequests(page);
  await page.goto("/mission?m=msn_not-a-mission-id");
  await expect(page).toHaveURL(/\/mission$/);
  // The console settles on its own default rather than the link.
  await expect(page.getByTestId("console-title")).toHaveText("First mission");
  expect(details.some((id) => id.includes("not-a-mission-id"))).toBe(false);
});
