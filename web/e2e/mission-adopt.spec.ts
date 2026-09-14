/** Adoption lives on the session (#948 P5): the sidebar row's ⋯ menu and the pane header.
 *
 * The mocks are STATEFUL — a successful adopt makes later `GET /api/sessions` reads carry the
 * mission — so a poll that happens to land mid-test agrees with the flip rather than reverting it.
 * The flip itself is asserted well inside the 15s poll interval, which is what proves it came from
 * the same-tab event and not from a refetch.
 */
import { expect, test, type Page } from "@playwright/test";
import { missionRow } from "./mission-console";
import { MISSION_PATH } from "../src/lib/missionLink";
import { setupBench } from "./terminal/harness";

const ENGINE = "claude";
const UUID = "bbbbbbbb-1111-2222-3333-444444444444";
const KEY = `${ENGINE}:${UUID}`;
const TITLE = "Fix the flaky upload retry";
const RUNNING = "msn_" + "a".repeat(32);
const DONE = "msn_" + "d".repeat(32);

type Held = { id: string; title: string; state: string } | null | "unknown";

function row(mission: Held) {
  return {
    id: KEY,
    engine: ENGINE,
    uuid: UUID,
    short_uuid: "bbbbbbbb",
    cwd: "/home/u/proj",
    project: { kind: "folder", id: "/home/u/proj", name: "/home/u/proj" },
    last_mtime: 1_700_000_000,
    first_user_message: "",
    title: TITLE,
    sticky: false,
    archived: false,
    // "unknown" = the server could not read the mission store, so the key is ABSENT.
    ...(mission === "unknown" ? {} : { mission }),
  };
}

async function setup(
  page: Page,
  opts: { initial?: Held; adopt?: { status: number; json: unknown } } = {},
) {
  await setupBench(page, { sessions: [{ engine: ENGINE, uuid: UUID, title: TITLE }] });
  let current: Held = opts.initial ?? null;
  const sessionUrls: string[] = [];
  await page.route(/\/api\/sessions(\?.*)?$/, (r) => {
    sessionUrls.push(r.request().url());
    const known = current !== "unknown";
    return r.fulfill({
      json: {
        sessions: [row(current)],
        next_offset: null,
        total: 1,
        facets: {
          projects: [],
          engines: [ENGINE],
          ...(known
            ? {
                missions: current ? [{ ...current, count: 1 }] : [],
                no_mission: current ? 0 : 1,
              }
            : {}),
        },
      },
    });
  });
  await page.route(/\/api\/missions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        missions: [
          missionRow({ id: RUNNING, title: "Harden the upload retry path", state: "running" }),
          missionRow({ id: DONE, title: "Theme Authentik", state: "done" }),
        ],
        total: 2,
        limit: 50,
        offset: 0,
        facets: { projects: [], states: [] },
        store_error: null,
      },
    }),
  );
  const posts: { url: string; body: unknown }[] = [];
  await page.route(/\/api\/missions\/msn_[0-9a-f]{32}\/adopt$/, async (r) => {
    posts.push({ url: r.request().url(), body: r.request().postDataJSON() });
    const res = opts.adopt ?? { status: 200, json: { id: RUNNING } };
    if (res.status === 200) {
      current = { id: RUNNING, title: "Harden the upload retry path", state: "running" };
    }
    return r.fulfill({ status: res.status, json: res.json });
  });
  return { posts, sessionUrls };
}

/** On a phone the session list is the shell's off-canvas drawer. Open it and wait for it to SETTLE
 *  on screen — a row trigger in a parked drawer resolves, but sits outside the viewport. No trigger
 *  means the docked desktop column, a genuine no-op. */
async function openSessionDrawer(page: Page) {
  const trigger = page.getByRole("button", { name: /Open session list/i });
  if (!(await trigger.count())) return;
  await trigger.first().click();
  await page.getByRole("dialog").waitFor({ state: "visible", timeout: 5000 });
  await page.waitForFunction(
    () => {
      const el = document.querySelector("aside.sidebar");
      return el !== null && el.getBoundingClientRect().x >= 0;
    },
    undefined,
    { timeout: 5000 },
  );
}

async function openRowMenu(page: Page) {
  await openSessionDrawer(page);
  await page.getByRole("button", { name: "Session actions" }).first().click();
}

test("adopt from the row menu: refusable missions say why, and the row flips to held", async ({
  page,
}) => {
  const { posts } = await setup(page);
  await page.goto("/");
  await openRowMenu(page);
  await page.getByRole("menuitem", { name: "Adopt session to a mission" }).click();

  const dialog = page.getByRole("dialog", { name: "Adopt to mission" });
  await expect(dialog).toBeVisible();
  const options = dialog.getByTestId("adopt-option");
  await expect(options).toHaveCount(2);
  // A done mission is listed, disabled, and says why — never silently filtered out.
  const done = options.filter({ hasText: "Theme Authentik" });
  await expect(done).toBeDisabled();
  await expect(done).toContainText("reopen it before adopting");
  await expect(dialog.getByTestId("adopt-confirm")).toBeDisabled();

  await options.filter({ hasText: "Harden the upload retry path" }).click();
  await dialog.getByTestId("adopt-confirm").click();
  await expect(dialog).toBeHidden();
  expect(posts).toHaveLength(1);
  expect(posts[0].url).toContain(`/api/missions/${RUNNING}/adopt`);
  expect(posts[0].body).toEqual({ session_key: KEY });

  // Held, without waiting for a poll.
  await expect(page.getByTestId("row-mission-tag").first()).toContainText(
    "Harden the upload retry path",
    { timeout: 5_000 },
  );
  // …and the menu now offers the way INTO that mission instead.
  await page.getByRole("button", { name: "Session actions" }).first().click();
  await expect(page.getByRole("menuitem", { name: "Adopt session to a mission" })).toHaveCount(0);
  await page.getByRole("menuitem", { name: "Open mission Harden the upload retry path" }).click();
  await expect.poll(() => new URL(page.url()).pathname).toBe(MISSION_PATH);
});

test("a refusal shows the server's own detail and keeps the dialog open", async ({ page }) => {
  await setup(page, {
    adopt: { status: 409, json: { detail: `session ${KEY} is held by mission ${DONE}` } },
  });
  await page.goto("/");
  await openRowMenu(page);
  await page.getByRole("menuitem", { name: "Adopt session to a mission" }).click();
  const dialog = page.getByRole("dialog", { name: "Adopt to mission" });
  await dialog.getByTestId("adopt-option").filter({ hasText: "Harden the upload retry path" }).click();
  await dialog.getByTestId("adopt-confirm").click();
  await expect(dialog.getByTestId("adopt-error")).toContainText(`is held by mission ${DONE}`);
  await expect(dialog).toBeVisible();
  await expect(page.getByTestId("row-mission-tag")).toHaveCount(0);
});

test("adopt from the pane header; the header then names the mission", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "the phone header is P6's Actions menu");
  await page.setViewportSize({ width: 1600, height: 900 });
  await setup(page);
  await page.goto(`/s/${ENGINE}/${UUID}`);
  await page.getByRole("button", { name: "Adopt this session into a mission" }).click();
  const dialog = page.getByRole("dialog", { name: "Adopt to mission" });
  await dialog.getByTestId("adopt-option").filter({ hasText: "Harden the upload retry path" }).click();
  await dialog.getByTestId("adopt-confirm").click();
  await expect(dialog).toBeHidden();
  await expect(
    page.getByRole("button", { name: "Open mission Harden the upload retry path" }),
  ).toBeVisible({ timeout: 5_000 });
  await expect(page.locator('[class*="panelHead"]').getByTestId("head-mission-tag")).toContainText(
    "Harden the upload retry path",
  );
  // Focus went back where the dialog was opened from — the control that now names the mission. The
  // action keeps its identity across the flip, so the opener is still mounted (#953 review).
  await expect(page.getByRole("button", { name: "Adopt this session into a mission" })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Open mission Harden the upload retry path" }),
  ).toBeFocused();
});

test("unknown membership offers neither adopt nor open", async ({ page }, testInfo) => {
  await setup(page, { initial: "unknown" });
  await page.goto("/");
  await openRowMenu(page);
  await expect(page.getByRole("menuitem", { name: /Adopt session|Open mission/ })).toHaveCount(0);
  await page.keyboard.press("Escape");
  if (testInfo.project.name === "desktop") {
    await page.goto(`/s/${ENGINE}/${UUID}`);
    await expect(page.getByRole("button", { name: "Session brief" }).or(page.getByRole("button", { name: "Open session brief" })).first()).toBeVisible();
    await expect(page.getByRole("button", { name: /Adopt this session|Open mission/ })).toHaveCount(0);
  }
});

test("filtering the list by mission asks the server, before pagination", async ({ page }) => {
  const { sessionUrls } = await setup(page);
  await page.goto("/");
  await openSessionDrawer(page);
  await page.getByLabel("Filter by mission").selectOption("none");
  await expect.poll(() => sessionUrls.some((u) => new URL(u).searchParams.get("mission") === "none")).toBe(true);
});

/** A fake `GET /api/missions` over `all`, paged by the request's offset/limit, whose snapshot is a
 *  digest of the ordered ids — so a removal between two pages changes it, as the server's does. */
function pagedMissions(all: () => ReturnType<typeof missionRow>[], urls: string[]) {
  return (r: import("@playwright/test").Route) => {
    const u = new URL(r.request().url());
    urls.push(u.search);
    const rows = all();
    const offset = Number(u.searchParams.get("offset") ?? 0);
    const limit = Number(u.searchParams.get("limit") ?? 50);
    return r.fulfill({
      json: {
        missions: rows.slice(offset, offset + limit),
        total: rows.length,
        limit,
        offset,
        facets: { projects: [], states: [] },
        store_error: null,
        snapshot: rows.map((m) => m.id).join(","),
      },
    });
  };
}

test("the picker restarts when the mission list moves between pages, instead of losing a mission", async ({
  page,
}) => {
  await setup(page);
  const hex = (n: number) => "msn_" + n.toString(16).padStart(32, "0");
  let missions = Array.from({ length: 51 }, (_, i) =>
    missionRow({ id: hex(i + 1), title: `Mission ${String(i + 1).padStart(2, "0")}`, state: "running" }),
  );
  const urls: string[] = [];
  await page.route(/\/api\/missions(\?.*)?$/, pagedMissions(() => missions, urls));
  await page.goto("/");
  await openRowMenu(page);
  await page.getByRole("menuitem", { name: "Adopt session to a mission" }).click();
  const dialog = page.getByRole("dialog", { name: "Adopt to mission" });
  await expect(dialog.getByTestId("adopt-option")).toHaveCount(50);

  // Another tab archives Mission 01 before Load more.
  missions = missions.slice(1);
  await dialog.getByRole("button", { name: /Load more/ }).click();

  await expect(dialog.getByTestId("adopt-option").filter({ hasText: "Mission 51" })).toBeVisible();
  await expect(dialog.getByTestId("adopt-option").filter({ hasText: "Mission 01" })).toHaveCount(0);
  await expect(dialog.getByTestId("adopt-option")).toHaveCount(50);
  await expect(dialog.getByRole("button", { name: /Load more/ })).toHaveCount(0);
});

test("an unreadable mission store is said, with a retry — not 'no open missions'", async ({ page }) => {
  await setup(page);
  let broken = true;
  await page.route(/\/api\/missions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        missions: broken ? [] : [missionRow({ id: RUNNING, title: "Harden the upload retry path", state: "running" })],
        total: broken ? 0 : 1,
        limit: 50,
        offset: 0,
        facets: { projects: [], states: [] },
        store_error: broken ? "database is locked" : null,
        snapshot: "s",
      },
    }),
  );
  await page.goto("/");
  await openRowMenu(page);
  await page.getByRole("menuitem", { name: "Adopt session to a mission" }).click();
  const dialog = page.getByRole("dialog", { name: "Adopt to mission" });
  await expect(dialog.getByTestId("adopt-load-error")).toContainText("database is locked");
  await expect(dialog.getByTestId("adopt-empty")).toHaveCount(0);
  broken = false;
  await dialog.getByRole("button", { name: "Retry" }).click();
  await expect(dialog.getByTestId("adopt-option")).toHaveCount(1);
});

/** 25 unassigned sessions under `mission=none`, 20 of them loaded, then the first one adopted.
 *
 *  THE 15s POLL IS OFF. It re-reads every loaded row, so left running it repairs the list on its own
 *  and an unfixed build passes whenever the steps take longer than one tick. With it off, only the
 *  adoption path can bring the list back in line with the server. `failReplacement` rejects the
 *  first offset-0 read after the adoption: the replacement read the adoption itself issues. */
async function adoptUnderNoneFilter(page: Page, opts: { failReplacement?: boolean } = {}) {
  await page.addInitScript(() => {
    const real = window.setInterval.bind(window);
    window.setInterval = ((fn: TimerHandler, ms?: number, ...rest: unknown[]) =>
      ms !== undefined && ms >= 15_000 ? 0 : real(fn, ms, ...rest)) as unknown as typeof window.setInterval;
  });
  await setupBench(page, { sessions: [{ engine: ENGINE, uuid: UUID, title: TITLE }] });
  const keys = Array.from({ length: 25 }, (_, i) => `${ENGINE}:bbbbbbbb-1111-2222-3333-${String(i).padStart(12, "0")}`);
  const adopted = new Set<string>();
  const offsets: number[] = [];
  let failNext = false;
  let replacementFailed = false;
  const sessionRow = (k: string, i: number) => ({
    ...row(adopted.has(k) ? { id: RUNNING, title: "Harden the upload retry path", state: "running" } : null),
    id: k,
    uuid: k.split(":")[1],
    short_uuid: k.split(":")[1].slice(0, 8),
    title: `Session ${String(i).padStart(2, "0")}`,
    last_mtime: 1_700_000_000 - i,
  });
  await page.route(/\/api\/sessions(\?.*)?$/, (r) => {
    const u = new URL(r.request().url());
    const offset = Number(u.searchParams.get("offset") ?? 0);
    const limit = Number(u.searchParams.get("limit") ?? 20);
    offsets.push(offset);
    if (failNext && offset === 0) {
      failNext = false;
      replacementFailed = true;
      return r.fulfill({ status: 500, json: { detail: "scan failed" } });
    }
    const none = u.searchParams.get("mission") === "none";
    const rows = keys.map(sessionRow).filter((s) => !none || s.mission === null);
    return r.fulfill({
      json: {
        sessions: rows.slice(offset, offset + limit),
        next_offset: offset + limit < rows.length ? offset + limit : null,
        total: rows.length,
        facets: { projects: [], engines: [ENGINE], missions: [], no_mission: 25 - adopted.size },
      },
    });
  });
  await page.route(/\/api\/missions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        missions: [missionRow({ id: RUNNING, title: "Harden the upload retry path", state: "running" })],
        total: 1, limit: 50, offset: 0, facets: { projects: [], states: [] }, store_error: null, snapshot: "s",
      },
    }),
  );
  await page.route(/\/api\/missions\/msn_[0-9a-f]{32}\/adopt$/, (r) => {
    adopted.add((r.request().postDataJSON() as { session_key: string }).session_key);
    failNext = Boolean(opts.failReplacement);
    return r.fulfill({ json: { id: RUNNING } });
  });

  await page.goto("/");
  await openSessionDrawer(page);
  await page.getByLabel("Filter by mission").selectOption("none");
  const list = page.locator("ul[aria-label$='sessions']");
  await expect(list.getByText("Session 19")).toBeVisible();
  await expect(list.getByText("Session 20")).toHaveCount(0);

  await page.getByRole("button", { name: "Session actions" }).first().click();
  await page.getByRole("menuitem", { name: "Adopt session to a mission" }).click();
  const dialog = page.getByRole("dialog", { name: "Adopt to mission" });
  await dialog.getByTestId("adopt-option").first().click();
  await dialog.getByTestId("adopt-confirm").click();
  await expect(dialog).toBeHidden();
  return { list, offsets, replacementFailed: () => replacementFailed };
}

test("adopting under 'Not in a mission' keeps Load more honest — no session is skipped", async ({ page }) => {
  const { list } = await adoptUnderNoneFilter(page);
  // The adopted session leaves this filter at once, and the next page starts where the server's
  // shrunken set now continues — Session 20 is not skipped.
  await expect(list.getByText("Session 00", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Load more" }).click();
  await expect(list.getByText("Session 20")).toBeVisible();
  await expect(list.getByText("Session 24")).toBeVisible();
  await expect(page.getByRole("button", { name: "Load more" })).toHaveCount(0);
});

test("a FAILED refresh after adoption does not re-arm the old cursor — Load more re-reads instead", async ({
  page,
}) => {
  const { list, offsets, replacementFailed } = await adoptUnderNoneFilter(page, { failReplacement: true });
  await expect.poll(replacementFailed).toBe(true);
  // The rows stay on screen: a failed replacement is not an error in place of the list.
  await expect(list.getByText("Session 19")).toBeVisible();

  // The cursor still describes the list before the adoption, so Load more must not append at
  // offset 20: that page would start at Session 21 and the list would call itself complete.
  await page.getByRole("button", { name: "Load more" }).click();
  await expect(list.getByText("Session 20")).toBeVisible();
  await expect(list.getByText("Session 24")).toBeVisible();
  await expect(list.getByText("Session 00", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Load more" })).toHaveCount(0);
  expect(offsets).not.toContain(20);
});
