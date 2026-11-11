/** HQ's Dispatches (#1837, #1746 P3a), in a real browser, desktop and phone, over the built SPA with
 *  `/api/**` mocked. The fake below keeps the server's rules (only WRITTEN bundles are listed,
 *  counted or readable; the cursor is `(line at, id)`, moved only forward and only to a written
 *  one), so what is under test is the client: what it links, what it scrolls to, which id it marks
 *  seen and WHEN — only on opening the view, never on a later read.
 *
 *  What a DOM emulator cannot prove: the bell's line is a link the operator can actually tap (44 px,
 *  on the phone's drawer too) and lands on `/hq/d/<id>` scrolled to the dispatch; every event's own
 *  Open link; "N new" across a reload, an open, an arrival while open and a prune; no sideways
 *  scroll; and the text contrast as laid out, in both themes. */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { HQ_PATH, hqDispatchPath } from "../src/lib/routes";
import { mockAskConversations } from "./askStream";
import { TOUCH_FLOOR } from "./touchTarget";

const NOW = Math.floor(Date.now() / 1000);
const UUID = "11111111-1111-4111-8111-111111111111";
const SESSION = `/s/claude/${UUID}`;
const bid = (n: number) => `d_${n.toString(16).padStart(16, "0")}`;

interface Member {
  class: string;
  title: string;
  reason: string;
  link: string;
  ts: number;
}
interface Bundle {
  id: string;
  /** null: not written (attempt, failed or gave up) — in the store, never a dispatch. */
  line: string | null;
  at: number;
  members: Member[];
}
interface Fake {
  bundles: Bundle[];
  seen: [number, string];
  seenPosts: string[];
  singleReads: string[];
  configured: boolean;
  /** The store's strict read fails: the list route answers as the server does then. */
  unreadable?: boolean;
  /** List reads made while set never answer (a hung request), or answer 500. */
  hold?: boolean;
  fail?: boolean;
  /** A race (Hermes 7623, finding 2): the NEXT list read is answered with the state as it was
   *  when it arrived, but only after the read that follows it has been answered; `mutate` changes
   *  the server in between. So an older-STARTED read lands after a newer one. */
  race?: {
    mutate: () => void;
    started?: boolean;
    done?: boolean;
    release?: () => void;
  } | null;
}

const member = (
  cls: string,
  title: string,
  link: string,
  ago = 120,
): Member => ({
  class: cls,
  title,
  reason: cls === "automation failed" ? "refused: the scope did not match" : "",
  link,
  ts: NOW - ago,
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
});

async function mockShell(
  page: Page,
  fake: Fake,
  notifications: unknown[] = [],
): Promise<void> {
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
    r.fulfill({
      json: {
        sessions: [],
        total: 0,
        next_offset: null,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route(/\/api\/pulse\/notifications$/, (r) =>
    r.fulfill({
      json: { notifications, unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({
      json: {
        missions: [],
        total: 0,
        next_offset: null,
        facets: { projects: [], states: [] },
      },
    }),
  );
  await mockAskConversations(page, []);
  // The Dispatches routes, with the server's rules.
  await page.route(/\/api\/hq\/dispatches(\/[^?]*)?(\?.*)?$/, async (r) => {
    const path = new URL(r.request().url()).pathname;
    const newCount = () =>
      written(fake).filter((b) => after(key(b), fake.seen)).length;
    if (path === "/api/hq/dispatches/seen" && r.request().method() === "POST") {
      const through =
        (JSON.parse(r.request().postData() || "{}") as { through?: string })
          .through ?? "";
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
      if (fake.hold) return new Promise<void>(() => undefined); // never answered
      if (fake.fail)
        return r.fulfill({ status: 500, json: { detail: "boom" } });
      if (fake.unreadable)
        return r.fulfill({
          json: { dispatches: [], new: 0, unreadable: true },
        });
      const list = [...written(fake)]
        .sort((a, b) => (after(key(a), key(b)) ? -1 : 1))
        .slice(0, 50);
      const json = {
        dispatches: list.map(view),
        new: newCount(),
        unreadable: false,
      };
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
    const b = written(fake).find((x) => x.id === id);
    return b
      ? r.fulfill({ json: { dispatch: view(b), new: newCount() } })
      : r.fulfill({
          status: 404,
          json: { detail: "that dispatch isn't here" },
        });
  });
}

function fakeOf(bundles: Bundle[]): Fake {
  return {
    bundles,
    seen: [0, ""],
    seenPosts: [],
    singleReads: [],
    configured: true,
  };
}

/** A bundle of three — each event with its OWN Open target — and a single needs-you dispatch. */
function twoDispatches(): Bundle[] {
  return [
    {
      id: bid(1),
      line: "Three in a minute: the nightly docs automation failed, a Claude Code session in api needs you, and the Release train mission escalated to you.",
      at: NOW - 600,
      members: [
        member(
          "automation failed",
          "Automation failed: nightly docs",
          "/mission/automations/nightlydocs",
        ),
        member("needs you", "docs: connectors guide", SESSION),
        member(
          "mission escalation",
          "ship checks: nothing moved",
          "/s/claude/22222222-2222-4222-8222-222222222222",
        ),
      ],
    },
    {
      id: bid(2),
      line: "A Claude Code session in agent-sessions is waiting on you.",
      at: NOW - 60,
      members: [member("needs you", "api: rebase onto main", SESSION, 60)],
    },
  ];
}

const isMobile = () => test.info().project.name === "mobile";
const entry = (page: Page) => page.getByTestId("hq-dispatches-entry");
const dispatch = (page: Page, id: string) =>
  page.locator(`[data-dispatch-id="${id}"]`);

/** Back to HQ's list: the phone shows it on its own screen. */
async function toList(page: Page): Promise<void> {
  if (isMobile()) await page.getByTestId("hq-dispatches-back").click();
  else await page.getByTestId("hq-list").getByTestId("hq-new").click();
  await expect(entry(page)).toBeVisible();
}

// ---- the bell -------------------------------------------------------------------------------------

test("the bell's line names its contact, links to /hq/d/<id>, and the row's Open is unchanged", async ({
  page,
}) => {
  const fake = fakeOf(twoDispatches());
  const rows = [
    {
      id: "n1",
      ts: NOW - 120,
      read: false,
      title: "docs: connectors guide",
      project: "docs-site",
      reason: "Asks you a question",
      session_id: `claude:${UUID}`,
      engine: "claude",
      action_id: "",
      dispatch: {
        id: bid(1),
        line: fake.bundles[0].line,
        by: "Gen. Okafor",
        bundled: 3,
      },
    },
    {
      id: "n2",
      ts: NOW - 30,
      read: false,
      title: "Automation failed: weekly report",
      project: "",
      reason: "failed: timeout",
      session_id: "",
      engine: "",
      action_id: "automation:weekly:r9",
    },
  ];
  await mockShell(page, fake, rows);
  await page.goto("/mission");
  await page.getByRole("button", { name: /^Notifications/ }).click();
  const lines = page.getByTestId("bell-hq-line");
  await expect(lines).toHaveCount(1); // the row without a line shows its fixed text only
  const line = lines.first();
  await expect(line).toContainText("Gen. Okafor wrote · 1 of 3 events");
  await expect(line).toContainText(fake.bundles[0].line!);
  await expect(line).toContainText("In HQ");
  await expect(line).toHaveAttribute("href", hqDispatchPath(bid(1)));
  expect((await line.boundingBox())!.height).toBeGreaterThanOrEqual(
    TOUCH_FLOOR,
  );
  // Each row's own Open is where it always went: its session, and the automation's run history.
  const opens = page.getByRole("link", { name: /^Open/ });
  await expect(opens).toHaveCount(2);
  const hrefs = await opens.evaluateAll((els) =>
    els.map((e) => e.getAttribute("href")),
  );
  expect(hrefs.sort()).toEqual(["/mission/automations/weekly", SESSION].sort());
  // The fixed reason stays, above the line.
  await expect(page.getByText("Asks you a question")).toBeVisible();

  await line.click();
  await expect(page).toHaveURL(new RegExp(`${hqDispatchPath(bid(1))}$`));
  await expect(dispatch(page, bid(1))).toHaveAttribute("data-linked", "true");
  await expect(page.getByTestId("hq-dispatch-linked")).toHaveText("Linked");
});

// ---- the view -------------------------------------------------------------------------------------

test("/hq/d/<id> scrolls to that dispatch and marks it LINKED, and only it", async ({
  page,
}) => {
  const many: Bundle[] = Array.from({ length: 9 }, (_, i) => ({
    id: bid(10 + i),
    line: `Dispatch number ${i}.`,
    at: NOW - 3000 + i * 300,
    members: [member("needs you", `session ${i}`, SESSION, 3000 - i * 300)],
  }));
  await mockShell(page, fakeOf(many));
  await page.setViewportSize(
    isMobile() ? { width: 412, height: 600 } : { width: 1280, height: 600 },
  );
  // One in the MIDDLE: neither the top (where an unscrolled list starts) nor the bottom.
  const middle = bid(14);
  await page.goto(hqDispatchPath(middle));
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(9);
  const linked = dispatch(page, middle);
  await expect(linked).toHaveAttribute("data-linked", "true");
  await expect(linked).toBeInViewport();
  await expect(page.locator('[data-linked="true"]')).toHaveCount(1);
  await expect(page.getByTestId("hq-dispatch-landed")).toContainText(
    "Opened from a link",
  );
  // Read only: no composer on this view.
  await expect(
    page.getByTestId("hq-dispatches").locator("textarea"),
  ).toHaveCount(0);
  await expect(
    page.getByText("To talk about a dispatch, ask in a conversation."),
  ).toBeVisible();
  // The newest of all is far below; and "All dispatches" drops the link.
  await page.getByTestId("hq-dispatches-all").click();
  await expect(page).toHaveURL(/\/hq\/d$/);
  await expect(page.locator('[data-linked="true"]')).toHaveCount(0);
});

test("every event in a bundle shows its OWN link, matching its row's Open target", async ({
  page,
}) => {
  const fake = fakeOf(twoDispatches());
  await mockShell(page, fake);
  await page.goto(hqDispatchPath(bid(1)));
  const members = dispatch(page, bid(1)).getByTestId("hq-dispatch-member");
  await expect(members).toHaveCount(3);
  const opens = members.getByTestId("hq-dispatch-open");
  await expect(opens).toHaveCount(3);
  expect(
    await opens.evaluateAll((els) =>
      els.map((e) => [e.textContent, e.getAttribute("href")]),
    ),
  ).toEqual([
    ["Open automation", "/mission/automations/nightlydocs"],
    ["Open session", SESSION],
    // An escalation opens its session, as its row does.
    ["Open session", "/s/claude/22222222-2222-4222-8222-222222222222"],
  ]);
  await expect(members.nth(0)).toContainText("Failed");
  await expect(members.nth(1)).toContainText("Needs you");
  await expect(members.nth(2)).toContainText("Escalated");
  await expect(dispatch(page, bid(1))).toContainText("Gen. Okafor wrote");
  await expect(
    dispatch(page, bid(1)).getByTestId("hq-dispatch-line"),
  ).toHaveText(fake.bundles[0].line!);
  await opens.nth(0).click();
  await expect(page).toHaveURL(/\/mission\/automations\/nightlydocs$/);
});

test("an unknown id reads 'isn't here'; a malformed one asks the server nothing", async ({
  page,
}) => {
  const fake = fakeOf(twoDispatches());
  // A bundle that FAILED: in the store for the at-most-once record, never a dispatch.
  fake.bundles.push({
    id: bid(99),
    line: null,
    at: NOW,
    members: [member("needs you", "x", SESSION)],
  });
  await mockShell(page, fake);
  for (const id of [bid(7), bid(99)]) {
    await page.goto(hqDispatchPath(id));
    await expect(page.getByTestId("hq-dispatch-missing")).toContainText(
      "This dispatch isn't here",
    );
    await expect(page.locator('[data-linked="true"]')).toHaveCount(0);
  }
  expect(fake.singleReads).toEqual([bid(7), bid(99)]);
  await page.goto("/hq/d/not-a-dispatch");
  await expect(page.getByTestId("hq-dispatch-missing")).toContainText(
    "This dispatch isn't here",
  );
  expect(fake.singleReads).toEqual([bid(7), bid(99)]);
});

// ---- the unread count -----------------------------------------------------------------------------

test("'N new' survives a reload, clears on opening, counts a line written while open, and survives a prune", async ({
  page,
}) => {
  const fake = fakeOf(twoDispatches());
  await mockShell(page, fake);
  await page.goto(HQ_PATH);
  await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveText(
    "2 new",
  );
  await page.reload();
  await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveText(
    "2 new",
  );

  // Opening the view marks the newest dispatch it RECEIVED as seen.
  await entry(page).click();
  await expect(page).toHaveURL(/\/hq\/d$/);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
  await expect.poll(() => fake.seenPosts).toEqual([bid(2)]);

  // While the view is open: one bundle fails, another is written.
  fake.bundles.push({
    id: bid(3),
    line: null,
    at: NOW + 5,
    members: [member("needs you", "f", SESSION)],
  });
  fake.bundles.push({
    id: bid(4),
    line: "The weekly report automation failed.",
    at: NOW + 10,
    members: [
      member(
        "automation failed",
        "Automation failed: weekly",
        "/mission/automations/weekly",
        0,
      ),
    ],
  });
  await toList(page);
  await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveText(
    "1 new",
  );
  expect(fake.seenPosts).toEqual([bid(2)]); // a later READ never marks anything seen

  // Pruning the oldest leaves the count right.
  fake.bundles = fake.bundles.filter((b) => b.id !== bid(1));
  await page.reload();
  await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveText(
    "1 new",
  );

  // The failed one is never listed, counted or linked; the written one is.
  await entry(page).click();
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
  await expect(dispatch(page, bid(3))).toHaveCount(0);
  await expect(dispatch(page, bid(4))).toBeVisible();
  await expect.poll(() => fake.seenPosts).toEqual([bid(2), bid(4)]);
  await toList(page);
  await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveCount(
    0,
  );
});

test("the entry is hidden while there are no dispatches", async ({ page }) => {
  await mockShell(page, fakeOf([]));
  await page.goto(HQ_PATH);
  await expect(page.getByTestId("hq-list")).toBeVisible();
  await expect(entry(page)).toHaveCount(0);
});

test("HQ not set up: dispatches already written stay readable", async ({
  page,
}) => {
  const fake = fakeOf(twoDispatches());
  fake.configured = false;
  await mockShell(page, fake);
  await page.goto(hqDispatchPath(bid(2)));
  await expect(page.getByTestId("hq-dispatches-not-set-up")).toContainText(
    "stay readable",
  );
  await expect(dispatch(page, bid(2))).toHaveAttribute("data-linked", "true");
});

// ---- an open page sees new dispatches (Hermes 7619, finding 4) -----------------------------------

test("an open HQ page shows the first dispatch without navigating, and an open view shows a new one unseen", async ({
  page,
}) => {
  await page.clock.install();
  const fake = fakeOf([]);
  await mockShell(page, fake);
  await page.goto(HQ_PATH);
  await expect(page.getByTestId("hq-list")).toBeVisible();
  await expect(entry(page)).toHaveCount(0);

  // The first line is written while the page is open: the entry appears on the next poll.
  fake.bundles.push(twoDispatches()[0]);
  await page.clock.runFor(16_000);
  await expect(entry(page)).toBeVisible();
  await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveText(
    "1 new",
  );

  // Open the view: it marks what it received as seen.
  await entry(page).click();
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(1);
  await expect.poll(() => fake.seenPosts).toEqual([bid(1)]);

  // A new bundle is written while the view is open: it appears, and is NOT marked seen.
  fake.bundles.push({ ...twoDispatches()[1], at: NOW + 30 });
  await page.clock.runFor(16_000);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
  await expect(dispatch(page, bid(2))).toBeVisible();
  expect(fake.seenPosts).toEqual([bid(1)]);
  if (!isMobile())
    await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveText(
      "1 new",
    );
  await toList(page);
  await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveText(
    "1 new",
  );
});

// ---- Hermes 7620: seen on entry only, scroll once, the poll is the current list -----------------

test("moving to All dispatches inside the view never marks an arrival seen", async ({
  page,
}) => {
  await page.clock.install();
  const fake = fakeOf(twoDispatches());
  await mockShell(page, fake);
  await page.goto(hqDispatchPath(bid(1)));
  await expect(dispatch(page, bid(1))).toHaveAttribute("data-linked", "true");
  await expect.poll(() => fake.seenPosts).toEqual([bid(2)]);
  // A dispatch arrives while the view is open, and the poll shows it.
  fake.bundles.push({ ...twoDispatches()[1], id: bid(5), at: NOW + 30 });
  await page.clock.runFor(16_000);
  await expect(dispatch(page, bid(5))).toBeVisible();
  // Navigating INSIDE the Dispatches re-reads, and sends no seen.
  await page.getByTestId("hq-dispatches-all").click();
  await expect(page).toHaveURL(/\/hq\/d$/);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(3);
  await page.clock.runFor(2_000);
  expect(fake.seenPosts).toEqual([bid(2)]);
  await toList(page);
  await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveText(
    "1 new",
  );
});

test("a poll never scrolls back to the linked dispatch", async ({ page }) => {
  await page.clock.install();
  const many: Bundle[] = Array.from({ length: 9 }, (_, i) => ({
    id: bid(10 + i),
    line: `Dispatch number ${i}.`,
    at: NOW - 3000 + i * 300,
    members: [member("needs you", `session ${i}`, SESSION, 3000 - i * 300)],
  }));
  const fake = fakeOf(many);
  await mockShell(page, fake);
  await page.setViewportSize(
    isMobile() ? { width: 412, height: 600 } : { width: 1280, height: 600 },
  );
  await page.goto(hqDispatchPath(bid(14)));
  await expect(dispatch(page, bid(14))).toBeInViewport();
  const scroller = page.getByTestId("hq-dispatches-scroll");
  // The Commander scrolls away to read the oldest.
  await scroller.evaluate((el) => (el.scrollTop = 0));
  await expect(dispatch(page, bid(14))).not.toBeInViewport();
  // A poll lands (it brings a newer dispatch at the far end, so its arrival is observable) and
  // hands back fresh objects for every dispatch, the linked one included.
  fake.bundles.push({ ...many[8], id: bid(30), at: NOW + 60 });
  await page.clock.runFor(16_000);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(10);
  await page.clock.runFor(2_000);
  expect(await scroller.evaluate((el) => el.scrollTop)).toBe(0);
  await expect(dispatch(page, bid(14))).not.toBeInViewport();
});

test("a pruned dispatch leaves an open view on the next poll; a pruned linked one isn't here", async ({
  page,
}) => {
  await page.clock.install();
  const fake = fakeOf(twoDispatches());
  await mockShell(page, fake);
  await page.goto("/hq/d");
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
  fake.bundles = fake.bundles.filter((b) => b.id !== bid(1));
  await page.clock.runFor(16_000);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(1);
  await expect(dispatch(page, bid(1))).toHaveCount(0);

  await page.goto(hqDispatchPath(bid(2)));
  await expect(dispatch(page, bid(2))).toHaveAttribute("data-linked", "true");
  fake.bundles = [];
  await page.clock.runFor(16_000);
  await expect(page.getByTestId("hq-dispatch-missing")).toContainText(
    "This dispatch isn't here",
  );
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(0);
});

test("a poll that can't read the store says so in an open view, never 'No dispatches yet'", async ({
  page,
}) => {
  // Hermes 7678, finding 2: a successful read, then an unreadable poll, then a readable one.
  await page.clock.install();
  const fake = fakeOf(twoDispatches());
  await mockShell(page, fake);
  await page.goto("/hq/d");
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
  fake.unreadable = true;
  await page.clock.runFor(16_000);
  await expect(page.getByTestId("hq-dispatches-unreadable")).toBeVisible();
  await expect(page.getByTestId("hq-dispatches-empty")).toHaveCount(0);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(0);
  fake.unreadable = false;
  await page.clock.runFor(16_000);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
  await expect(page.getByTestId("hq-dispatches-unreadable")).toHaveCount(0);
});

test("a view opened while the store can't be read recovers on the next readable poll", async ({
  page,
}) => {
  // Hermes 7681, finding 3: the view's OWN read is the unreadable one.
  await page.clock.install();
  const fake = fakeOf(twoDispatches());
  fake.unreadable = true;
  await mockShell(page, fake);
  await page.goto("/hq/d");
  await expect(page.getByTestId("hq-dispatches-unreadable")).toBeVisible();
  fake.unreadable = false;
  await page.clock.runFor(16_000);
  await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
  await expect(page.getByTestId("hq-dispatches-unreadable")).toHaveCount(0);
});

test("the pinned entry stays through an unreadable poll, says so, and recovers", async ({
  page,
}) => {
  // Hermes 7681, finding 4: a passing read failure is never shown as "no dispatches".
  await page.clock.install();
  const fake = fakeOf(twoDispatches());
  await mockShell(page, fake);
  await page.goto(HQ_PATH);
  await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveText(
    "2 new",
  );
  fake.unreadable = true;
  await page.clock.runFor(16_000);
  await expect(entry(page).getByTestId("hq-dispatches-entry-meta")).toHaveText(
    "Can't be read right now",
  );
  await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveCount(
    0,
  );
  fake.unreadable = false;
  await page.clock.runFor(16_000);
  await expect(
    entry(page).getByTestId("hq-dispatches-entry-meta"),
  ).toContainText("Written by HQ");
  await expect(entry(page).getByTestId("hq-dispatches-new-count")).toHaveText(
    "2 new",
  );
});

test("a store that can't be read from the start still shows the entry, saying so", async ({
  page,
}) => {
  const fake = fakeOf([]);
  fake.unreadable = true;
  await mockShell(page, fake);
  await page.goto(HQ_PATH);
  await expect(entry(page).getByTestId("hq-dispatches-entry-meta")).toHaveText(
    "Can't be read right now",
  );
});

for (const mode of ["hold", "fail"] as const)
  test(`a view whose own first read ${mode === "hold" ? "hangs" : "fails"} is settled by the next healthy poll`, async ({
    page,
  }) => {
    // Hermes 7743, finding 3: the page's poll succeeds while the view's own read never does.
    await page.clock.install();
    const fake = fakeOf(twoDispatches());
    fake[mode] = true;
    await mockShell(page, fake);
    await page.goto("/hq/d");
    const stuck =
      mode === "hold"
        ? page.getByText("Reading dispatches…", { exact: true })
        : page.getByTestId("hq-dispatches-failed");
    await expect(stuck).toBeVisible();
    fake[mode] = false;
    await page.clock.runFor(16_000);
    await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
    await expect(stuck).toHaveCount(0);
    expect(fake.seenPosts).toEqual([]); // a poll never marks anything seen
  });

for (const target of ["older than the list", "unknown"] as const)
  test(`a deep link whose own first read hangs still resolves once a poll settles the view: ${target}`, async ({
    page,
  }) => {
    // Hermes 7956: the linked dispatch is outside the poll's 50 newest (or not a dispatch at all),
    // and the view's own read, which would have looked it up, never answers.
    await page.clock.install();
    const many: Bundle[] = Array.from({ length: 51 }, (_, i) => ({
      id: bid(100 + i),
      line: `Dispatch number ${i}.`,
      at: NOW - 6000 + i * 60,
      members: [member("needs you", `session ${i}`, SESSION, 6000 - i * 60)],
    }));
    const fake = fakeOf(many);
    fake.hold = true;
    await mockShell(page, fake);
    const linkedId = target === "unknown" ? bid(7) : bid(100); // bid(100): the oldest
    await page.goto(hqDispatchPath(linkedId));
    await expect(
      page.getByText("Reading dispatches…", { exact: true }),
    ).toBeVisible();
    fake.hold = false;
    await page.clock.runFor(16_000);
    if (target === "unknown") {
      await expect(page.getByTestId("hq-dispatch-missing")).toContainText(
        "This dispatch isn't here",
      );
      await expect(page.locator('[data-linked="true"]')).toHaveCount(0);
    } else {
      await expect(page.getByTestId("hq-dispatch")).toHaveCount(51);
      await expect(dispatch(page, linkedId)).toHaveAttribute(
        "data-linked",
        "true",
      );
      await expect(page.getByTestId("hq-dispatch-linked")).toHaveText("Linked");
    }
    expect(fake.singleReads).toContain(linkedId);
    expect(fake.seenPosts).toEqual([]); // a poll never marks anything seen
  });

// ---- Hermes 7623: reads are ordered by when they STARTED ------------------------------------------

for (const change of ["an arrival", "a prune"] as const) {
  test(`an older-started list read never rolls back a newer one: ${change}`, async ({
    page,
  }) => {
    const fake = fakeOf(twoDispatches());
    await mockShell(page, fake);
    await page.goto(hqDispatchPath(bid(1)));
    await expect(dispatch(page, bid(1))).toHaveAttribute("data-linked", "true");
    await expect.poll(() => fake.seenPosts).toEqual([bid(2)]);
    // Linked → All: the view and the page both read; the FIRST read is answered last, with the
    // state from before the change, and the second (started later) sees the change.
    fake.race = {
      mutate: () => {
        if (change === "an arrival")
          fake.bundles.push({
            ...twoDispatches()[1],
            id: bid(5),
            at: NOW + 30,
          });
        else fake.bundles = fake.bundles.filter((b) => b.id !== bid(1));
      },
    };
    await page.getByTestId("hq-dispatches-all").click();
    await expect(page).toHaveURL(/\/hq\/d$/);
    await expect.poll(() => fake.race?.done ?? false).toBe(true);
    // Both answers are in (the held one, the view's own, lands last). Read the screen once, NOT
    // with a retrying assertion: the next 15 s poll would heal a rolled-back list and hide it.
    await page.waitForTimeout(400);
    const ids = await page
      .getByTestId("hq-dispatch")
      .evaluateAll((els) => els.map((e) => e.getAttribute("data-dispatch-id")));
    expect(ids.sort()).toEqual(
      change === "an arrival" ? [bid(1), bid(2), bid(5)].sort() : [bid(2)],
    );
  });
}

// ---- touch targets, sideways scroll, contrast -----------------------------------------------------

/** WCAG contrast of an element's text against the colour `ground` paints. */
async function contrast(l: Locator, ground: Locator): Promise<number> {
  const fg = await l.evaluate((el) => getComputedStyle(el).color);
  const bg = await ground.evaluate(
    (el) => getComputedStyle(el).backgroundColor,
  );
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

for (const theme of ["dark", "light"] as const) {
  test(`44 px targets, no sideways scroll, and AA text (${theme})`, async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: theme });
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    const fake = fakeOf(twoDispatches());
    await mockShell(page, fake, [
      {
        id: "n1",
        ts: NOW - 120,
        read: false,
        title: "docs: connectors guide",
        project: "docs-site",
        reason: "Asks you a question",
        session_id: `claude:${UUID}`,
        engine: "claude",
        action_id: "",
        dispatch: {
          id: bid(1),
          line: fake.bundles[0].line,
          by: "Gen. Okafor",
          bundled: 3,
        },
      },
    ]);
    await page.goto(hqDispatchPath(bid(1)));
    const v = page.getByTestId("hq-dispatches");
    await expect(page.getByTestId("hq-dispatch")).toHaveCount(2);
    const targets = [
      ...(await v.getByTestId("hq-dispatch-open").all()),
      v.getByTestId("hq-dispatches-all"),
      v.getByTestId("hq-dispatches-new"),
      ...(isMobile() ? [v.getByTestId("hq-dispatches-back")] : [entry(page)]),
    ];
    for (const t of targets) {
      const box = await t.boundingBox();
      expect(
        box!.height,
        await t.evaluate((e) => e.outerHTML.slice(0, 80)),
      ).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    const card = dispatch(page, bid(1));
    const face = card.locator(".nj-face");
    const fixed = card.getByTestId("hq-dispatch-member").first();
    for (const [text, ground] of [
      [card.getByTestId("hq-dispatch-line"), face],
      [card.getByText("Gen. Okafor wrote"), face],
      [fixed.getByText("Automation failed: nightly docs"), fixed],
      [fixed.getByText("Failed", { exact: true }), fixed],
      [fixed.getByText(/^Automation · /), fixed],
      [fixed.getByText("refused: the scope did not match"), fixed],
    ] as const)
      expect(
        await contrast(text, ground),
        await text.evaluate((e) => e.outerHTML.slice(0, 60)),
      ).toBeGreaterThanOrEqual(4.5);
    // The bell's line, as laid out in its panel (the phone's drawer too).
    await page.getByRole("button", { name: /^Notifications/ }).click();
    const line = page.getByTestId("bell-hq-line");
    expect((await line.boundingBox())!.height).toBeGreaterThanOrEqual(
      TOUCH_FLOOR,
    );
    for (const sel of [
      "span:nth-child(1)",
      "span:nth-child(2)",
      "span:nth-child(3)",
    ])
      expect(await contrast(line.locator(sel), line)).toBeGreaterThanOrEqual(
        4.5,
      );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  });
}
