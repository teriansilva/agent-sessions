/** HQ's page (#1776, Phase 1), in a real browser, desktop and mobile, on the direct origin.
 *
 *  What a DOM emulator cannot prove: the URL the browser actually shows after the first send, a
 *  pick, Back/Forward, a delete and a reload; that the sidebar's Open as page lands on the same
 *  conversation and stays disabled while a draft or a staged file would be lost; that a turn the
 *  hidden sidebar is still streaming shows on the page as pending, holds the page's Send, and
 *  reaches the server exactly once; that an unknown and a deleted id render the same state; and the
 *  touch targets and text contrast as laid out. Signed-out → login (+ TOTP) → the same path needs
 *  the real server's pages: `appmode-hq-page.spec.ts`.
 *
 *  Red on main: `/hq/c/<id>` redirected to the dashboard with the sidebar open. */
import { expect, test, type Locator, type Page, type Route } from "@playwright/test";

import { HQ_PATH, hqConversationPath } from "../src/lib/routes";
import { openAsk } from "./askSidebar";
import {
  ASK_STREAM,
  fulfillAsk,
  mockAskConversations,
  turnOf,
  type FakeAsk,
  type FakeConversation,
} from "./askStream";
import { TOUCH_FLOOR } from "./touchTarget";

const A = "ask_" + "a".repeat(32);
const B = "ask_" + "b".repeat(32);

function conversations(): FakeConversation[] {
  return [
    {
      id: A,
      created_at: 10,
      updated_at: 30,
      turns: [turnOf("alpha question", { answer: "the alpha answer" })],
    },
    {
      id: B,
      created_at: 5,
      updated_at: 20,
      turns: [turnOf("bravo question", { answer: "the bravo answer" })],
    },
  ];
}

async function mockShell(page: Page, seed = conversations()): Promise<FakeAsk> {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({
      json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } },
    }),
  );
  return mockAskConversations(page, seed);
}

const isMobile = () => test.info().project.name === "mobile";
const thread = (page: Page) => page.getByTestId("hq-thread");
const list = (page: Page) => page.getByTestId("hq-list");

/** A conversation on the page: on a phone the list is one screen and the thread another. */
async function pickOnPage(page: Page, title: string): Promise<void> {
  if (isMobile() && !(await list(page).isVisible())) await thread(page).getByTestId("ask-back").click();
  await list(page).getByTestId("hq-list-item").filter({ hasText: title }).click();
}

test("/hq/c/<id> opens that conversation, and a reload keeps it (#1776)", async ({ page }) => {
  await mockShell(page);
  await page.goto(hqConversationPath(B));
  await expect(page).toHaveURL(new RegExp(`${hqConversationPath(B)}$`));
  await expect(thread(page)).toBeVisible();
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the bravo answer");
  // The sidebar never opens beside the page: one console per conversation.
  await expect(page.getByTestId("ask-sidebar")).toHaveCount(0);
  if (!isMobile()) {
    await expect(list(page).locator('[aria-current="page"]')).toContainText("bravo question");
  }
  await page.reload();
  await expect(page).toHaveURL(new RegExp(`${hqConversationPath(B)}$`));
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the bravo answer");
});

test("the first send replaces /hq with /hq/c/<id>; Back never returns to the empty draft", async ({
  page,
}) => {
  const fake = await mockShell(page, []);
  await page.route(ASK_STREAM, (r) => fulfillAsk(r, { answer: "first answer" }));
  await page.goto(HQ_PATH);
  await expect(list(page).getByTestId("hq-list-empty")).toBeVisible();
  if (isMobile()) {
    // A phone lands on the list; New conversation is the thread before it has an id.
    await expect(thread(page)).toBeHidden();
    await list(page).getByTestId("hq-new").click();
  }
  await expect(page).toHaveURL(/\/hq$/);
  const input = thread(page).getByTestId("composer-input");
  await input.fill("the first question");
  await input.press("Enter");
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("first answer");
  const created = fake.conversations[0].id;
  await expect(page).toHaveURL(new RegExp(`${hqConversationPath(created)}$`));
  await page.goBack();
  if (isMobile()) {
    // Back from the thread is the list — never the empty draft the send replaced.
    await expect(page).toHaveURL(/\/hq$/);
    await expect(list(page)).toBeVisible();
    await expect(thread(page)).toBeHidden();
  } else {
    // The empty draft was replaced: Back leaves the page (here, the blank tab it started from).
    await expect.poll(() => page.url()).not.toMatch(/\/hq$/);
  }
});

test("a pick pushes; Back and Forward move between conversations", async ({ page }) => {
  await mockShell(page);
  await page.goto(hqConversationPath(A));
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the alpha answer");
  await pickOnPage(page, "bravo question");
  await expect(page).toHaveURL(new RegExp(`${hqConversationPath(B)}$`));
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the bravo answer");
  await page.goBack();
  if (isMobile()) {
    // A phone went thread → list → thread: Back is the list first.
    await expect(page).toHaveURL(/\/hq$/);
    await page.goBack();
  }
  await expect(page).toHaveURL(new RegExp(`${hqConversationPath(A)}$`));
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the alpha answer");
  await page.goForward();
  if (isMobile()) await page.goForward();
  await expect(page).toHaveURL(new RegExp(`${hqConversationPath(B)}$`));
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the bravo answer");
});

test("unsent text stays with its conversation across a pick, Back and Forward (review 7393)", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto(hqConversationPath(A));
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the alpha answer");
  const input = thread(page).getByTestId("composer-input");
  await input.fill("private draft for alpha");
  await pickOnPage(page, "bravo question");
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the bravo answer");
  await expect(input).toHaveValue(""); // never A's words in B's box
  // Back to A (through the list on a phone): its draft is where it was typed.
  for (let i = 0; i < 3 && !page.url().endsWith(hqConversationPath(A)); i++) await page.goBack();
  await expect(page).toHaveURL(new RegExp(`${hqConversationPath(A)}$`));
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the alpha answer");
  await expect(input).toHaveValue("private draft for alpha");
  for (let i = 0; i < 3 && !page.url().endsWith(hqConversationPath(B)); i++) await page.goForward();
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the bravo answer");
  await expect(input).toHaveValue("");
});

test("deleting the conversation on screen returns to /hq, and its id then reads 'isn't here'", async ({
  page,
}) => {
  const fake = await mockShell(page);
  await page.goto(hqConversationPath(A));
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the alpha answer");
  await thread(page).getByTestId("ask-delete").click();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page).toHaveURL(/\/hq$/);
  expect(fake.deleted).toEqual([A]);
  if (isMobile()) await expect(list(page)).toBeVisible();
  await expect(list(page).getByTestId("hq-list-item")).toHaveCount(1);
  await expect(list(page)).not.toContainText("alpha question");

  // The deleted id and one that never existed read the same.
  await page.goto(hqConversationPath(A));
  const deleted = await page.getByTestId("hq-missing").innerText();
  await page.goto(hqConversationPath("ask_" + "f".repeat(32)));
  await expect(page.getByTestId("hq-missing")).toBeVisible();
  expect(await page.getByTestId("hq-missing").innerText()).toBe(deleted);
  // …and so does a link that is not an id at all, which asks the server nothing.
  const asked: string[] = [];
  page.on("request", (r) => {
    if (/\/api\/ask\/conversations\/[^/]+$/.test(new URL(r.url()).pathname)) asked.push(r.url());
  });
  await page.goto("/hq/c/not-a-conversation");
  await expect(page.getByTestId("hq-missing")).toBeVisible();
  expect(await page.getByTestId("hq-missing").innerText()).toBe(deleted);
  expect(asked).toEqual([]);
});

test("Open as page: the same conversation, /hq from an empty thread, disabled with a draft or a file", async ({
  page,
}) => {
  await mockShell(page);
  await page.route(/\/api\/ask\/conversations\/ask_[0-9a-f]{32}\/attachments$/, (r) =>
    r.fulfill({
      status: 201,
      json: { attachment: { id: "att_" + "1".repeat(32), name: "notes.txt", kind: "text", size: 5 } },
    }),
  );
  await page.route(/\/api\/ask\/conversations\/ask_[0-9a-f]{32}\/attachments\/.+$/, (r) =>
    r.fulfill({ json: { discarded: true } }),
  );
  await page.goto("/dashboard");

  // From the empty thread: the page itself, and nothing is minted.
  let panel = await openAsk(page);
  const openPage = panel.getByTestId("ask-open-page");
  await expect(openPage).toBeEnabled();
  await openPage.click();
  await expect(page).toHaveURL(/\/hq$/);
  await expect(page.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "false");

  // A draft holds it, with its reason.
  await page.goto("/dashboard");
  panel = await openAsk(page);
  const { pickConversation } = await import("./askSidebar");
  await pickConversation(panel, 1);
  await expect(panel.getByTestId("ask-answer")).toHaveText("the bravo answer");
  await panel.getByTestId("composer-input").fill("half a thought");
  await expect(panel.getByTestId("ask-open-page")).toBeDisabled();
  await expect(panel.getByTestId("ask-open-page")).toHaveAttribute(
    "title",
    "Send or clear your draft first",
  );
  await panel.getByTestId("composer-input").fill("");
  await expect(panel.getByTestId("ask-open-page")).toBeEnabled();
  // …and so does a staged file.
  await panel
    .locator('input[type="file"]')
    .first()
    .setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("hello") });
  await expect(panel.getByTestId("ask-open-page")).toBeDisabled();
  await panel.getByRole("button", { name: /remove notes\.txt/i }).click();
  await expect(panel.getByTestId("ask-open-page")).toBeEnabled();

  // On a kept conversation: that conversation's page, and the panel closes.
  await panel.getByTestId("ask-open-page").click();
  await expect(page).toHaveURL(new RegExp(`${hqConversationPath(B)}$`));
  await expect(thread(page).getByTestId("ask-answer")).toHaveText("the bravo answer");
  await expect(page.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "false");
});

test("in-flight handoff: a turn the sidebar is streaming shows pending on the page, holds Send, reaches the server once", async ({
  page,
}) => {
  const fake = await mockShell(page);
  let release!: () => void;
  const held = new Promise<void>((r) => (release = r));
  const posts: string[] = [];
  await page.route(ASK_STREAM, async (r: Route) => {
    const req = r.request().postDataJSON() as { turn_id: string; text: string };
    posts.push(req.text);
    const id = /\/(ask_[0-9a-f]{32})\/message\/stream$/.exec(new URL(r.request().url()).pathname)![1];
    const c = fake.conversations.find((x) => x.id === id)!;
    // The server holds the turn as PENDING while it answers.
    c.turns.push({ ...turnOf(req.text, { answer: "" }, req.turn_id), status: "pending", answer: null });
    await held;
    await fulfillAsk(r, { answer: "the settled answer" });
  });
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  const { pickConversation } = await import("./askSidebar");
  await pickConversation(panel, 0);
  await panel.getByTestId("composer-input").fill("still thinking?");
  await panel.getByTestId("composer-input").press("Enter");
  await expect(panel.getByRole("article", { name: /Answer from/ }).last()).toHaveAttribute(
    "aria-busy",
    "true",
  );
  await expect.poll(() => posts.length).toBe(1);

  // Hand off mid-stream: Open as page.
  await panel.getByTestId("ask-open-page").click();
  await expect(page).toHaveURL(new RegExp(`${hqConversationPath(A)}$`));
  const answers = thread(page).getByRole("article", { name: /Answer from/ });
  await expect(answers.last()).toHaveAttribute("aria-busy", "true");
  const input = thread(page).getByTestId("composer-input");
  await input.fill("one more");
  const send = thread(page).getByRole("button", { name: "Send", exact: true });
  await expect(send).toBeDisabled();
  await input.press("Enter");
  expect(posts).toEqual(["still thinking?"]);

  // The sidebar's stream settles; the page reads it, and only then may it send.
  release();
  await expect(answers.last()).toHaveAttribute("aria-busy", "false");
  await expect(thread(page).getByTestId("ask-answer").last()).toHaveText("the settled answer");
  await expect(send).toBeEnabled();
  expect(posts).toEqual(["still thinking?"]);
  const c = fake.conversations.find((x) => x.id === A)!;
  expect(c.turns.filter((t) => t.question === "still thinking?")).toHaveLength(1);
});

test("on the page the corner icon goes to the composer, never the sidebar; /ask is the page", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto("/ask");
  await expect(page).toHaveURL(/\/hq$/);
  await page.locator('.hud-topbar [data-testid="ask-toggle"]').click();
  await expect(page.getByTestId("ask-sidebar")).toHaveCount(0);
  await expect(thread(page).getByTestId("composer-input")).toBeFocused();
});

/** WCAG contrast of an element's text against the colour its column paints. */
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

for (const theme of ["dark", "light"] as const) {
  test(`the list's controls clear the touch floor and its text holds AA contrast (${theme})`, async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: theme });
    await mockShell(page);
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await page.goto(HQ_PATH);
    const l = list(page);
    await expect(l.getByTestId("hq-list-item")).toHaveCount(2);
    for (const target of [
      l.getByTestId("hq-new"),
      // The field the operator taps: the whole labelled box focuses the input.
      l.getByTestId("hq-search").locator(".."),
      ...(await l.getByTestId("hq-list-item").all()),
    ]) {
      const box = await target.boundingBox();
      expect(box!.height, await target.evaluate((e) => e.outerHTML.slice(0, 80))).toBeGreaterThanOrEqual(
        TOUCH_FLOOR,
      );
    }
    for (const text of [
      l.getByText("alpha question"),
      l.getByTestId("hq-list-item").first().locator("span").nth(1),
    ])
      expect(await contrast(text, l)).toBeGreaterThanOrEqual(4.5);
    // No sideways scroll on a phone.
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
      true,
    );
  });
}
