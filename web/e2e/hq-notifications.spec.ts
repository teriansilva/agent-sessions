/** HQ // Notifications (#2146, was HQ's Dispatches #1837), in a real browser, desktop and phone, over
 *  the built SPA with `/api/**` mocked. The fake keeps the server's rules: only WRITTEN bundles are
 *  listed or readable; the cursor is `(line at, id)`, moved only forward and only to a written one;
 *  a bell row carries `dispatch: {id, line, by, bundled, tag}` only while its bundle is written;
 *  Mark all read and Clear all act on the bell's rows and nothing else. What is under test is the
 *  client: the join by event tag, what it links, what it scrolls to, which id it marks seen and
 *  WHEN (only on entering the view), and what the bulk actions count.
 *
 *  What a DOM emulator cannot prove: the bell's line and footer are links the operator can tap
 *  (44 px, the phone's drawer too) and land on `/hq/d/<id>` scrolled to the bundle; every event's
 *  own Open; the polls; no sideways scroll; the text contrast as laid out, in both themes. */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { HQ_DISPATCHES_PATH, HQ_PATH, hqDispatchPath } from "../src/lib/routes";
import { mockAskConversations } from "./askStream";
import { TOUCH_FLOOR } from "./touchTarget";

const NOW = Math.floor(Date.now() / 1000);
const UUID = "11111111-1111-4111-8111-111111111111";
const UUID2 = "22222222-2222-4222-8222-222222222222";
const SESSION = `/s/claude/${UUID}`;
const bid = (n: number) => `d_${n.toString(16).padStart(16, "0")}`;
const nyTag = (ep: number, uuid = UUID) => `needs-you:claude:${uuid}:${ep.toString(16)}`;

interface Member {
  class: string;
  title: string;
  reason: string;
  link: string;
  ts: number;
  tag: string;
}
interface Bundle {
  id: string;
  /** null: not written (attempt, failed or gave up) — in the store, never a dispatch. */
  line: string | null;
  at: number;
  members: Member[];
  /** Where the same events went (#1841); absent when the route ledger can't be read. */
  routes?: unknown[];
}
interface Row {
  id: string;
  ts: number;
  read: boolean;
  title: string;
  project: string;
  reason: string;
  session_id: string;
  engine: string;
  action_id: string;
  needs_you?: boolean;
  escalation?: boolean;
  /** The row's own stored event tag (the server's `dispatch`): joined only to a written bundle. */
  tag?: string;
}
interface Fake {
  bundles: Bundle[];
  rows: Row[];
  seen: [number, string];
  seenPosts: string[];
  singleReads: string[];
  readPosts: unknown[];
  dismissPosts: unknown[];
  configured: boolean;
  /** The store's strict read fails: the list route answers as the server does then. */
  unreadable?: boolean;
  /** The notifications read answers 500. */
  failNotifications?: boolean;
  /** Single-dispatch reads made while set answer 500 (or never, `holdSingle`). */
  failSingle?: boolean;
  holdSingle?: boolean;
  /** A race (Hermes 7623): the NEXT list read is answered with the state as it was when it arrived,
   *  but only after the read that follows it has been answered; `mutate` changes the server in
   *  between. So an older-STARTED read lands after a newer one. */
  race?: { mutate: () => void; started?: boolean; done?: boolean; release?: () => void } | null;
}

const member = (cls: string, title: string, link: string, tag: string, ago = 120): Member => ({
  class: cls,
  title,
  reason: cls === "automation failed" ? "refused: the scope did not match" : "",
  link,
  ts: NOW - ago,
  tag,
});

const written = (f: Fake) => f.bundles.filter((b) => b.line !== null);
const key = (b: Bundle): [number, string] => [b.at, b.id];
const after = (a: [number, string], b: [number, string]) =>
  a[0] > b[0] || (a[0] === b[0] && a[1] > b[1]);
const view = (b: Bundle) => ({
  id: b.id,
  at: b.at,
  opened_at: b.at - 60,
  line: b.line,
  contact: "General Amara Okafor",
  by: "Gen. Okafor",
  members: b.members,
  ...(b.routes ? { routes: b.routes } : {}),
});

/** The bell's listing as `join_listing` makes it: a row's tag joined only to a written bundle. */
function listing(f: Fake) {
  const notifications = [...f.rows]
    .sort((a, b) => b.ts - a.ts)
    .map(({ tag, ...r }) => {
      const b = tag ? written(f).find((x) => x.members.some((m) => m.tag === tag)) : undefined;
      return b
        ? { ...r, dispatch: { id: b.id, line: b.line, by: "Gen. Okafor", bundled: b.members.length, tag } }
        : r;
    });
  return { notifications, unread: f.rows.filter((r) => !r.read).length, uncertain: 0, settled: [] };
}

async function mockShell(page: Page, fake: Fake): Promise<void> {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: fake.configured },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  // The bell's rows, with the server's bulk actions: they touch the rows and nothing else.
  await page.route(/\/api\/pulse\/notifications(\/[a-z-]+)?$/, (r) => {
    const path = new URL(r.request().url()).pathname;
    if (path.endsWith("/read")) {
      fake.readPosts.push(r.request().postDataJSON());
      for (const row of fake.rows) row.read = true;
      return r.fulfill({ json: listing(fake) });
    }
    if (path.endsWith("/dismiss")) {
      const body = r.request().postDataJSON() as { ids?: string[]; all?: boolean };
      fake.dismissPosts.push(body);
      fake.rows = body.all ? [] : fake.rows.filter((x) => !body.ids?.includes(x.id));
      return r.fulfill({ json: listing(fake) });
    }
    if (fake.failNotifications) return r.fulfill({ status: 500, json: { detail: "boom" } });
    return r.fulfill({ json: listing(fake) });
  });
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }),
  );
  await mockAskConversations(page, []);
  // HQ's lines, with the server's rules.
  await page.route(/\/api\/hq\/dispatches(\/[^?]*)?(\?.*)?$/, async (r) => {
    const path = new URL(r.request().url()).pathname;
    const newCount = () => written(fake).filter((b) => after(key(b), fake.seen)).length;
    if (path === "/api/hq/dispatches/seen" && r.request().method() === "POST") {
      const through = (r.request().postDataJSON() as { through?: string }).through ?? "";
      fake.seenPosts.push(through);
      const b = written(fake).find((x) => x.id === through);
      let moved = false;
      if (b && after(key(b), fake.seen)) {
        fake.seen = key(b);
        moved = true;
      }
      return r.fulfill({ json: { moved, new: newCount() } });
    }
    if (path === "/api/hq/dispatches") {
      if (fake.unreadable) return r.fulfill({ json: { dispatches: [], new: 0, unreadable: true } });
      const list = [...written(fake)].sort((a, b) => (after(key(a), key(b)) ? -1 : 1)).slice(0, 50);
      const json = { dispatches: list.map(view), new: newCount(), unreadable: false };
      const race = fake.race;
      if (race && !race.started) {
        race.started = true;
        const held = new Promise<void>((res) => (race.release = res));
        race.mutate();
        await held;
        return r.fulfill({ json });
      }
      await r.fulfill({ json });
      if (race && !race.done) {
        race.done = true;
        race.release?.();
      }
      return;
    }
    const id = decodeURIComponent(path.split("/").pop() ?? "");
    fake.singleReads.push(id);
    if (fake.holdSingle) return new Promise<void>(() => undefined); // never answered
    if (fake.failSingle) return r.fulfill({ status: 500, json: { detail: "boom" } });
    const b = written(fake).find((x) => x.id === id);
    return b
      ? r.fulfill({ json: { dispatch: view(b), new: newCount() } })
      : r.fulfill({ status: 404, json: { detail: "that dispatch isn't here" } });
  });
}

function fakeOf(bundles: Bundle[], rows: Row[] = []): Fake {
  return {
    bundles,
    rows,
    seen: [0, ""],
    seenPosts: [],
    singleReads: [],
    readPosts: [],
    dismissPosts: [],
    configured: true,
  };
}

const AUTO_TAG = "automation:nightlydocs:r1";
const ESC_TAG = `escalation:msn_${"a".repeat(32)}:${"b".repeat(12)}:1`;

/** A bundle of three — each event with its OWN Open target — and a single needs-you dispatch. */
function twoDispatches(): Bundle[] {
  return [
    {
      id: bid(1),
      line: "Three in a minute: the nightly docs automation failed, a Claude Code session in api needs you, and the Release train mission escalated to you.",
      at: NOW - 600,
      members: [
        member("automation failed", "Automation failed: nightly docs", "/mission/automations/nightlydocs", AUTO_TAG, 700),
        member("needs you", "docs: connectors guide", SESSION, nyTag(1), 680),
        member("mission escalation", "ship checks: nothing moved", `/s/claude/${UUID2}`, ESC_TAG, 720),
      ],
    },
    {
      id: bid(2),
      line: "A Claude Code session in agent-sessions is waiting on you.",
      at: NOW - 60,
      members: [member("needs you", "api: rebase onto main", SESSION, nyTag(2), 90)],
    },
  ];
}

/** Live bell rows for two of bundle 1's members (the escalation's row is gone), bundle 2's one,
 *  and a row HQ wrote nothing about. */
function liveRows(): Row[] {
  return [
    { id: "n-auto", ts: NOW - 700, read: true, title: "Automation failed: nightly docs", project: "", reason: "failed: boom", session_id: "", engine: "", action_id: AUTO_TAG, tag: AUTO_TAG },
    { id: "n-docs", ts: NOW - 680, read: false, title: "docs: connectors guide", project: "docs-site", reason: "Asks you a question", session_id: `claude:${UUID}`, engine: "claude", action_id: "", needs_you: true, tag: nyTag(1) },
    { id: "n-api", ts: NOW - 90, read: false, title: "api: rebase onto main", project: "agent-sessions", reason: "Waiting for your approval", session_id: `claude:${UUID}`, engine: "claude", action_id: "", needs_you: true, tag: nyTag(2) },
    { id: "n-solo", ts: NOW - 30, read: false, title: "Automation failed: weekly report", project: "", reason: "failed: timeout", session_id: "", engine: "", action_id: "automation:weekly:r9" },
  ]; // prettier-ignore
}

const isMobile = () => test.info().project.name === "mobile";
const bundleOf = (page: Page, id: string) => page.locator(`[data-dispatch-id="${id}"]`);
const rows = (l: Locator) => l.getByTestId("notification-row");
const filter = (page: Page, name: string) =>
  page.getByRole("group", { name: "Show" }).getByRole("button", { name: new RegExp(`^${name}`) });

// ---- the bell -------------------------------------------------------------------------------------

test("the bell sits a bundle's rows together, draws HQ's line once, and links to the view", async ({ page }) => {
  const fake = fakeOf(twoDispatches(), liveRows());
  await mockShell(page, fake);
  await page.goto("/mission");
  await page.getByRole("button", { name: /^Notifications/ }).click();
  const lines = page.getByTestId("bell-hq-line");
  // One line per bundle, never one per row: bundle 1 has two live rows, bundle 2 one.
  await expect(lines).toHaveCount(2);
  const line = page.locator(`[data-testid="bell-hq-line"][href="${hqDispatchPath(bid(1))}"]`);
  await expect(line).toContainText("Gen. Okafor wrote · 3 events");
  await expect(line).toContainText(fake.bundles[0].line!);
  await expect(line).toContainText("In HQ");
  expect((await line.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // Bundle 1's two rows are adjacent, and its line follows the second.
  const order = await page
    .getByRole("dialog", { name: "Notifications" })
    .locator("li")
    .evaluateAll((els) => els.map((e) => e.textContent ?? ""));
  const at = (s: string) => order.findIndex((t) => t.includes(s));
  expect(at("Automation failed: nightly docs")).toBe(at("docs: connectors guide") + 1);
  expect(at("Gen. Okafor wrote · 3 events")).toBe(at("Automation failed: nightly docs") + 1);
  // Each row's own Open is where it always went.
  const hrefs = await page
    .getByRole("dialog", { name: "Notifications" })
    .getByRole("link", { name: /^Open/ })
    .evaluateAll((els) => els.map((e) => e.getAttribute("href")));
  expect(hrefs.sort()).toEqual(
    ["/mission/automations/nightlydocs", "/mission/automations/weekly", SESSION, SESSION].sort(),
  );
  // All notifications → the view.
  const all = page.getByTestId("bell-all-notifications");
  expect((await all.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await all.click();
  await expect(page).toHaveURL(/\/hq\/d$/);
  await expect(page.getByRole("heading", { level: 1, name: "Notifications" })).toBeVisible();
  // In HQ → the view at that bundle.
  await page.getByRole("button", { name: /^Notifications/ }).click();
  await page.locator(`[data-testid="bell-hq-line"][href="${hqDispatchPath(bid(1))}"]`).click();
  await expect(page).toHaveURL(new RegExp(`${hqDispatchPath(bid(1))}$`));
  await expect(bundleOf(page, bid(1))).toHaveAttribute("data-linked", "true");
  await expect(page.getByTestId("hq-dispatch-linked")).toHaveText("Linked");
});

// ---- the view: one list ---------------------------------------------------------------------------

test("every event once: live rows inside their bundle, the gone member from its snapshot, HQ's line on top", async ({ page }) => {
  const fake = fakeOf(twoDispatches(), liveRows());
  await mockShell(page, fake);
  await page.goto(HQ_DISPATCHES_PATH);
  const v = page.getByTestId("hq-notifications");
  await expect(v.getByText("HQ // Notifications")).toBeVisible();
  // 4 live rows + 1 snapshot member, each once.
  await expect(rows(v)).toHaveCount(5);
  const b1 = bundleOf(page, bid(1));
  await expect(b1.getByTestId("hq-dispatch-line")).toHaveText(fake.bundles[0].line!);
  await expect(b1).toContainText("General Amara Okafor wrote · 3 events");
  // Newest first inside the bundle; the escalation's row was dismissed: its snapshot stands in.
  const inB1 = rows(b1);
  await expect(inB1).toHaveCount(3);
  await expect(inB1.nth(0)).toContainText("docs: connectors guide");
  await expect(inB1.nth(1)).toContainText("Automation failed: nightly docs");
  await expect(inB1.nth(2)).toContainText("ship checks: nothing moved");
  await expect(inB1.nth(2).getByTestId("notification-gone")).toHaveText("No longer in the bell");
  await expect(inB1.nth(2).getByRole("button", { name: /^Dismiss/ })).toHaveCount(0);
  // Live rows keep their ✕ and their read state.
  await expect(inB1.nth(0).getByRole("button", { name: /^Dismiss/ })).toBeVisible();
  await expect(inB1.nth(0)).toHaveAttribute("data-unread", "true");
  await expect(inB1.nth(2)).not.toHaveAttribute("data-unread", "true");
  // Each event's own Open, matching where its row goes.
  expect(
    await inB1.getByRole("link", { name: /^Open/ }).evaluateAll((els) => els.map((e) => [e.textContent, e.getAttribute("href")])),
  ).toEqual([
    ["Open session", SESSION],
    ["Open automation", "/mission/automations/nightlydocs"],
    ["Open session", `/s/claude/${UUID2}`],
  ]);
  await expect(inB1.nth(0)).toContainText("Needs you");
  await expect(inB1.nth(1)).toContainText("Failed");
  await expect(inB1.nth(2)).toContainText("Escalated");
  // The row HQ wrote nothing about stands alone, newest first.
  const order = await page
    .getByTestId("notifications-feed")
    .getByTestId("notification-row")
    .evaluateAll((els) => els.map((e) => e.querySelector("div")?.textContent));
  expect(order[0]).toBe("Automation failed: weekly report");
  // Read only: no composer here. Opening the view refreshes; it marks nothing read.
  await expect(v.locator("textarea")).toHaveCount(0);
  await page.waitForTimeout(300);
  expect(fake.readPosts).toEqual([]);
});

test("filters and counts: a snapshot member is under All and its class, never Unread", async ({ page }) => {
  const fake = fakeOf(twoDispatches(), liveRows());
  await mockShell(page, fake);
  await page.goto(HQ_DISPATCHES_PATH);
  await expect(rows(page.getByTestId("hq-notifications"))).toHaveCount(5);
  for (const [name, n] of [["All", 5], ["Unread", 3], ["Needs you", 2], ["Escalations", 1], ["Automations", 2]] as const)
    await expect(filter(page, name)).toContainText(String(n));
  await filter(page, "Unread").click();
  await expect(filter(page, "Unread")).toHaveAttribute("aria-pressed", "true");
  await expect(rows(page.getByTestId("notifications-feed"))).toHaveCount(3);
  await expect(page.getByTestId("notification-gone")).toHaveCount(0);
  await filter(page, "Escalations").click();
  const esc = rows(page.getByTestId("notifications-feed"));
  await expect(esc).toHaveCount(1);
  await expect(esc.first().getByTestId("notification-gone")).toBeVisible();
  // The bundle keeps its line while any of its events match.
  await expect(bundleOf(page, bid(1)).getByTestId("hq-dispatch-line")).toBeVisible();
  await expect(page.getByTestId("notifications-unread")).toHaveText("3 unread");
});

test("Mark all read and Clear all act on the bell's rows only; HQ's lines and snapshots stay", async ({ page }) => {
  const fake = fakeOf(twoDispatches(), liveRows());
  await mockShell(page, fake);
  await page.goto(HQ_DISPATCHES_PATH);
  await expect(rows(page.getByTestId("hq-notifications"))).toHaveCount(5);
  await page.getByRole("button", { name: "Mark all read" }).click();
  await expect.poll(() => fake.readPosts.length).toBe(1);
  await expect(filter(page, "Unread")).toContainText("0");
  await expect(page.locator('[data-unread="true"]')).toHaveCount(0);
  // Clear all arms in place; its count is the 4 live rows, never the view's 5; its words say how
  // long HQ's lines are kept — 30 days OR the 2,000 cap, never "30 days" alone.
  await page.getByRole("button", { name: "Clear all" }).click();
  const confirm = page.getByTestId("notifications-clear-confirm");
  await expect(confirm).toHaveText(
    "Clear 4 notifications from the bell? HQ's lines stay in this view (kept up to 30 days, at most 2,000).",
  );
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(confirm).toHaveCount(0);
  expect(fake.dismissPosts).toEqual([]);
  await page.getByRole("button", { name: "Clear all" }).click();
  await page.getByRole("button", { name: "Yes, clear" }).click();
  await expect.poll(() => fake.dismissPosts).toEqual([{ all: true }]);
  // Every bell row is gone; every bundle member is still listed from its snapshot.
  const left = rows(page.getByTestId("notifications-feed"));
  await expect(left).toHaveCount(4);
  await expect(page.getByTestId("notification-gone")).toHaveCount(4);
  await expect(page.getByText("Automation failed: weekly report")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Clear all" })).toHaveCount(0);
  await expect(filter(page, "All")).toContainText("4");
});

test("dismissing one row in the view deletes it in the bell; its event stays under HQ's line", async ({ page }) => {
  const fake = fakeOf(twoDispatches(), liveRows());
  await mockShell(page, fake);
  await page.goto(HQ_DISPATCHES_PATH);
  await bundleOf(page, bid(2)).getByRole("button", { name: "Dismiss: api: rebase onto main" }).click();
  await expect.poll(() => fake.dismissPosts).toEqual([{ ids: ["n-api"] }]);
  await expect(bundleOf(page, bid(2)).getByTestId("notification-gone")).toBeVisible();
});

// ---- links ----------------------------------------------------------------------------------------

test("/hq/d/<id> scrolls to that bundle and marks it LINKED, and only it", async ({ page }) => {
  const many: Bundle[] = Array.from({ length: 9 }, (_, i) => ({
    id: bid(10 + i),
    line: `Dispatch number ${i}.`,
    at: NOW - 30000 + i * 3000,
    members: [member("needs you", `session ${i}`, SESSION, nyTag(10 + i), 30000 - i * 3000)],
  }));
  await mockShell(page, fakeOf(many));
  await page.setViewportSize(isMobile() ? { width: 412, height: 600 } : { width: 1280, height: 600 });
  const middle = bid(14);
  await page.goto(hqDispatchPath(middle));
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(9);
  const linked = bundleOf(page, middle);
  await expect(linked).toHaveAttribute("data-linked", "true");
  await expect(linked).toBeInViewport();
  await expect(page.locator('[data-linked="true"]')).toHaveCount(1);
  await expect(page.getByTestId("hq-dispatch-landed")).toContainText("Opened from a link");
  await page.getByTestId("hq-dispatches-all").click();
  await expect(page).toHaveURL(/\/hq\/d$/);
  await expect(page.locator('[data-linked="true"]')).toHaveCount(0);
});

test("each bundle says where the same events went: sent, resolved, or skipped (#1841)", async ({ page }) => {
  const bundles = twoDispatches();
  bundles[0].routes = [
    { instance: "c_1", connector: "Telegram", state: "retracted", sent: true, resolved: true },
    { instance: "c_2", connector: "Email (SMTP)", state: "skipped_quiet", sent: false, resolved: false },
  ]; // prettier-ignore
  await mockShell(page, fakeOf(bundles));
  await page.goto(hqDispatchPath(bid(1)));
  await expect(bundleOf(page, bid(1)).getByTestId("hq-dispatch-routes")).toHaveText(
    "Sent to Telegram · sent · ✓ resolved; Email (SMTP) · skipped · quiet hours",
  );
  await expect(bundleOf(page, bid(2)).getByTestId("hq-dispatch-routes")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("an unknown or unwritten id isn't here; a malformed one asks the server nothing", async ({ page }) => {
  const fake = fakeOf(twoDispatches());
  fake.bundles.push({ id: bid(99), line: null, at: NOW, members: [member("needs you", "x", SESSION, nyTag(99))] });
  await mockShell(page, fake);
  for (const id of [bid(7), bid(99)]) {
    await page.goto(hqDispatchPath(id));
    await expect(page.getByTestId("hq-dispatch-missing")).toContainText("This dispatch isn't here");
    // The retention, accurately: 30 days OR the 2,000 cap.
    await expect(page.getByTestId("hq-dispatch-missing")).toContainText("kept up to 30 days, at most 2,000");
    await expect(page.locator('[data-linked="true"]')).toHaveCount(0);
    // The rest is still there.
    await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
  }
  expect(fake.singleReads).toEqual([bid(7), bid(99)]);
  await page.goto("/hq/d/not-a-dispatch");
  await expect(page.getByTestId("hq-dispatch-missing")).toContainText("This dispatch isn't here");
  expect(fake.singleReads).toEqual([bid(7), bid(99)]);
});

test("HQ not set up: lines already written stay readable", async ({ page }) => {
  const fake = fakeOf(twoDispatches());
  fake.configured = false;
  await mockShell(page, fake);
  await page.goto(hqDispatchPath(bid(2)));
  await expect(page.getByTestId("hq-dispatches-not-set-up")).toContainText("stay readable");
  await expect(bundleOf(page, bid(2))).toHaveAttribute("data-linked", "true");
});

// ---- bundles beyond the newest 50 -----------------------------------------------------------------

test("a live row whose bundle is past the newest 50 is read once on its own; a failed read never hides it", async ({ page }) => {
  const many: Bundle[] = Array.from({ length: 51 }, (_, i) => ({
    id: bid(100 + i),
    line: `Dispatch number ${i}.`,
    at: NOW - 60000 + i * 60,
    members: [
      member("needs you", `session ${i}`, SESSION, nyTag(100 + i), 60000 - i * 60),
      member("automation failed", `auto ${i}`, "/mission/automations/a", `automation:a:${i}`, 60000 - i * 60 + 1),
    ],
  }));
  const oldest = many[0];
  // Two live rows of the OLDEST bundle (not in the newest 50): one lookup, not two.
  const live: Row[] = oldest.members.map((m, k) => ({
    id: `old-${k}`, ts: m.ts, read: false, title: m.title, project: "", reason: "", session_id: k ? "" : `claude:${UUID}`,
    engine: k ? "" : "claude", action_id: k ? m.tag : "", tag: m.tag,
  })); // prettier-ignore
  const fake = fakeOf(many, live);
  fake.failSingle = true;
  await mockShell(page, fake);
  await page.goto(HQ_DISPATCHES_PATH);
  // The lookup failed: the rows still show, grouped under the line the bell's own join gave them.
  const b = bundleOf(page, oldest.id);
  await expect(rows(b)).toHaveCount(2);
  await expect(b.getByTestId("hq-dispatch-line")).toHaveText(oldest.line!);
  await expect(b).toContainText("Gen. Okafor wrote · 2 events");
  // Deduplicated: ONE read for the bundle both rows name.
  await expect.poll(() => fake.singleReads).toEqual([oldest.id]);
  fake.failSingle = false;
  fake.singleReads = [];
  await page.reload();
  // Read: the contact's full line, from the dispatch itself.
  await expect(bundleOf(page, oldest.id)).toContainText("General Amara Okafor wrote · 2 events");
  await expect(rows(bundleOf(page, oldest.id))).toHaveCount(2);
  expect(fake.singleReads).toEqual([oldest.id]);
});

// ---- the seen cursor, the poll --------------------------------------------------------------------

test("seen is sent once on entry; a line written while open appears and is not marked seen", async ({ page }) => {
  await page.clock.install();
  const fake = fakeOf(twoDispatches());
  await mockShell(page, fake);
  await page.goto(hqDispatchPath(bid(1)));
  await expect(bundleOf(page, bid(1))).toHaveAttribute("data-linked", "true");
  await expect.poll(() => fake.seenPosts).toEqual([bid(2)]);
  fake.bundles.push({ ...twoDispatches()[1], id: bid(5), at: NOW + 30, members: [member("needs you", "new one", SESSION, nyTag(5), -30)] });
  await page.clock.runFor(16_000);
  await expect(bundleOf(page, bid(5))).toBeVisible();
  // Navigating INSIDE the view sends no seen.
  await page.getByTestId("hq-dispatches-all").click();
  await expect(page).toHaveURL(/\/hq\/d$/);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(3);
  await page.clock.runFor(2_000);
  expect(fake.seenPosts).toEqual([bid(2)]);
  // Leaving and coming back is a new entry: it sends the newest it received.
  await page.goto(HQ_PATH);
  await page.goto(HQ_DISPATCHES_PATH);
  await expect.poll(() => fake.seenPosts).toEqual([bid(2), bid(5)]);
});

test("a poll never scrolls back to the linked bundle", async ({ page }) => {
  await page.clock.install();
  const many: Bundle[] = Array.from({ length: 9 }, (_, i) => ({
    id: bid(10 + i),
    line: `Dispatch number ${i}.`,
    at: NOW - 30000 + i * 3000,
    members: [member("needs you", `session ${i}`, SESSION, nyTag(10 + i), 30000 - i * 3000)],
  }));
  const fake = fakeOf(many);
  await mockShell(page, fake);
  await page.setViewportSize(isMobile() ? { width: 412, height: 600 } : { width: 1280, height: 600 });
  await page.goto(hqDispatchPath(bid(14)));
  await expect(bundleOf(page, bid(14))).toBeInViewport();
  const scroller = page.getByTestId("hq-notifications");
  await scroller.evaluate((el) => (el.scrollTop = 0));
  await expect(bundleOf(page, bid(14))).not.toBeInViewport();
  fake.bundles.push({ ...many[8], id: bid(30), at: NOW + 60 });
  await page.clock.runFor(16_000);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(10);
  await page.clock.runFor(2_000);
  expect(await scroller.evaluate((el) => el.scrollTop)).toBe(0);
  await expect(bundleOf(page, bid(14))).not.toBeInViewport();
});

test("a pruned bundle leaves an open view on the next poll; a pruned linked one isn't here", async ({ page }) => {
  await page.clock.install();
  const fake = fakeOf(twoDispatches());
  await mockShell(page, fake);
  await page.goto(HQ_DISPATCHES_PATH);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
  fake.bundles = fake.bundles.filter((b) => b.id !== bid(1));
  await page.clock.runFor(16_000);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(1);
  await page.goto(hqDispatchPath(bid(2)));
  await expect(bundleOf(page, bid(2))).toHaveAttribute("data-linked", "true");
  fake.bundles = [];
  await page.clock.runFor(16_000);
  await expect(page.getByTestId("hq-dispatch-missing")).toContainText("This dispatch isn't here");
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(0);
});

test("an older-started list read never rolls back a newer one", async ({ page }) => {
  await page.clock.install();
  const fake = fakeOf(twoDispatches());
  // The view's first read is held and answered with the state from before the arrival, AFTER the
  // poll that started later (and saw the arrival) has been answered.
  fake.race = {
    mutate: () => fake.bundles.push({ ...twoDispatches()[1], id: bid(5), at: NOW + 30 }),
  };
  await mockShell(page, fake);
  await page.goto(HQ_DISPATCHES_PATH);
  await expect.poll(() => fake.race?.started ?? false).toBe(true);
  await page.clock.runFor(16_000);
  await expect.poll(() => fake.race?.done ?? false).toBe(true);
  await expect(bundleOf(page, bid(5))).toBeVisible();
  // Read once, not with a retrying assertion: the next poll would heal a rolled-back list.
  await page.clock.runFor(400);
  const ids = await page.getByTestId("hq-dispatch").evaluateAll((els) => els.map((e) => e.getAttribute("data-dispatch-id")));
  expect(ids.sort()).toEqual([bid(1), bid(2), bid(5)].sort());
});

// ---- degraded reads -------------------------------------------------------------------------------

test("HQ's lines unreadable: every row still shows, and the view says so", async ({ page }) => {
  await page.clock.install();
  const fake = fakeOf(twoDispatches(), liveRows());
  fake.unreadable = true;
  // The server's own join drops the lines then: the rows come ungrouped.
  const keep = fake.bundles;
  fake.bundles = [];
  await mockShell(page, fake);
  await page.goto(HQ_DISPATCHES_PATH);
  await expect(page.getByTestId("hq-dispatches-unreadable")).toHaveText(
    "HQ's lines can't be read right now. Notifications are unaffected.",
  );
  await expect(rows(page.getByTestId("notifications-feed"))).toHaveCount(4);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(0);
  fake.unreadable = false;
  fake.bundles = keep;
  await page.clock.runFor(61_000);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
  await expect(page.getByTestId("hq-dispatches-unreadable")).toHaveCount(0);
});

test("a failed notifications read says so with Retry, never the empty line", async ({ page }) => {
  const fake = fakeOf(twoDispatches(), liveRows());
  fake.failNotifications = true;
  await mockShell(page, fake);
  await page.goto(HQ_DISPATCHES_PATH);
  const err = page.getByTestId("notifications-failed");
  await expect(err).toContainText("Couldn't read the notifications.");
  await expect(page.getByTestId("notifications-empty")).toHaveCount(0);
  // Unknown is not zero: the counts say so.
  await expect(filter(page, "All")).toContainText("–");
  fake.failNotifications = false;
  await err.getByRole("button", { name: "Retry" }).click();
  await expect(err).toHaveCount(0);
  await expect(rows(page.getByTestId("notifications-feed"))).toHaveCount(5);
});

test("a successful empty read, then a failed refresh: never the all-clear (Hermes 8452)", async ({ page }) => {
  await page.clock.install();
  const fake = fakeOf([]);
  await mockShell(page, fake);
  await page.goto(HQ_DISPATCHES_PATH);
  await expect(page.getByTestId("notifications-empty")).toHaveText("Nothing needs you right now.");
  fake.failNotifications = true;
  await page.clock.runFor(61_000); // the bell store's next poll fails
  await expect(page.getByTestId("notifications-failed")).toContainText("Couldn't read the notifications.");
  await expect(page.getByTestId("notifications-empty")).toHaveCount(0);
  fake.failNotifications = false;
  await page.getByTestId("notifications-failed").getByRole("button", { name: "Retry" }).click();
  await expect(page.getByTestId("notifications-empty")).toBeVisible();
});

test("a bundle read on its own is re-checked after leaving its link, and goes when pruned (Hermes 8452)", async ({ page }) => {
  await page.clock.install();
  const many: Bundle[] = Array.from({ length: 51 }, (_, i) => ({
    id: bid(100 + i),
    line: `Dispatch number ${i}.`,
    at: NOW - 60000 + i * 60,
    members: [member("needs you", `session ${i}`, SESSION, nyTag(100 + i), 60000 - i * 60)],
  }));
  const oldest = many[0].id; // outside the newest 50: read on its own
  const fake = fakeOf(many);
  await mockShell(page, fake);
  await page.goto(hqDispatchPath(oldest));
  await expect(bundleOf(page, oldest)).toHaveAttribute("data-linked", "true");
  await page.getByTestId("hq-dispatches-all").click();
  await expect(page).toHaveURL(/\/hq\/d$/);
  await expect(bundleOf(page, oldest)).toBeVisible();
  // Pruned server-side: a later poll re-checks the cached bundle, and its 404 removes it.
  fake.bundles = fake.bundles.filter((b) => b.id !== oldest);
  fake.singleReads = [];
  await page.clock.runFor(16_000);
  await expect(bundleOf(page, oldest)).toHaveCount(0);
  expect(fake.singleReads).toEqual([oldest]);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(50);
});

test("nothing at all: the bell's empty line", async ({ page }) => {
  await mockShell(page, fakeOf([]));
  await page.goto(HQ_DISPATCHES_PATH);
  await expect(page.getByTestId("notifications-empty")).toHaveText("Nothing needs you right now.");
  await expect(page.getByRole("button", { name: "Clear all" })).toHaveCount(0);
});

// ---- touch targets, sideways scroll, contrast, captures -------------------------------------------

/** WCAG contrast of an element's text against the colour `ground` paints. */
async function contrast(l: Locator, ground: Locator): Promise<number> {
  const fg = await l.evaluate((el) => getComputedStyle(el).color);
  const bg = await ground.evaluate((el) => getComputedStyle(el).backgroundColor);
  return l.evaluate(
    (_el, [fgs, bgs]) => {
      const rgb = (s: string) => {
        const n = (s.match(/[\d.]+/g) || []).map(Number).slice(0, 3);
        return s.startsWith("color(") ? n.map((v) => v * 255) : n;
      };
      const lum = ([r, g, b]: number[]) => {
        const f = (v: number) => {
          const c = v / 255;
          return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
        };
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
      };
      const [a, b] = [lum(rgb(fgs)), lum(rgb(bgs))].sort((x, y) => y - x);
      return (a + 0.05) / (b + 0.05);
    },
    [fg, bg] as [string, string],
  );
}

/** The first ancestor (or self) that paints an opaque background. */
async function ground(l: Locator): Promise<Locator> {
  const depth = await l.evaluate((el) => {
    let n: Element | null = el;
    let d = 0;
    while (n) {
      // Opaque only: a translucent tint (an unread row's) is not what the text sits on alone.
      const nums = (getComputedStyle(n).backgroundColor.match(/[\d.]+/g) || []).map(Number);
      if (nums.length === 3 || (nums.length >= 4 && nums[3] === 1)) return d;
      n = n.parentElement;
      d += 1;
    }
    return d;
  });
  return depth === 0 ? l : l.locator(`xpath=${Array(depth).fill("..").join("/")}`);
}

for (const theme of ["dark", "light"] as const) {
  test(`44 px targets, no sideways scroll, AA text, and captures (${theme})`, async ({ page }, info) => {
    await page.emulateMedia({ colorScheme: theme });
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await page.setViewportSize(isMobile() ? { width: 390, height: 844 } : { width: 1440, height: 900 });
    const fake = fakeOf(twoDispatches(), liveRows());
    await mockShell(page, fake);
    await page.goto(hqDispatchPath(bid(1)));
    const v = page.getByTestId("hq-notifications");
    await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: info.outputPath(`notifications-linked-${theme}.png`), fullPage: true });
    const targets = [
      ...(await v.getByRole("link", { name: /^Open/ }).all()),
      ...(await v.getByRole("button", { name: /^Dismiss/ }).all()),
      ...(await page.getByRole("group", { name: "Show" }).getByRole("button").all()),
      v.getByRole("button", { name: "Mark all read" }),
      v.getByRole("button", { name: "Clear all" }),
      v.getByTestId("hq-dispatches-all"),
    ];
    for (const t of targets) {
      const box = await t.boundingBox();
      // A pointer may get 32 px chrome; a phone's touch floor is 44 px everywhere.
      expect(box!.height, await t.evaluate((e) => e.outerHTML.slice(0, 80))).toBeGreaterThanOrEqual(
        isMobile() ? TOUCH_FLOOR : 32,
      );
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const b1 = bundleOf(page, bid(1));
    const gone = rows(b1).nth(2);
    for (const text of [
      b1.getByTestId("hq-dispatch-line"),
      b1.getByText("General Amara Okafor wrote · 3 events"),
      rows(b1).nth(0).getByText("docs: connectors guide"),
      rows(b1).nth(0).getByText("Needs you", { exact: true }),
      rows(b1).nth(1).getByText("Failed", { exact: true }),
      gone.getByText("ship checks: nothing moved"),
      gone.getByText("Escalated", { exact: true }),
    ])
      expect(await contrast(text, await ground(text)), await text.evaluate((e) => e.outerHTML.slice(0, 60))).toBeGreaterThanOrEqual(4.5);
    await page.getByRole("button", { name: "Clear all" }).click();
    await page.screenshot({ path: info.outputPath(`notifications-clear-armed-${theme}.png`) });
    await page.getByRole("button", { name: "Cancel" }).click();
    // The bell's line and footer, as laid out in its panel (the phone's drawer too).
    await page.getByRole("button", { name: /^Notifications/ }).click();
    const line = page.getByTestId("bell-hq-line").first();
    expect((await line.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    for (const sel of ["span:nth-child(1)", "span:nth-child(2)", "span:nth-child(3)"])
      expect(await contrast(line.locator(sel), line)).toBeGreaterThanOrEqual(4.5);
    const foot = page.getByTestId("bell-all-notifications");
    expect((await foot.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(await contrast(foot, foot)).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: info.outputPath(`bell-${theme}.png`) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
