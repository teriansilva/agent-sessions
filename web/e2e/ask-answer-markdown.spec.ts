/** Ask's answers render their markdown, in a real browser, desktop and mobile.
 *
 *  The answer model writes markdown (bullets, bold labels, an italic aside, code). It used to land
 *  as raw text — `- **Find sessions** —` with the stars showing. Now it goes through `blockMarkup`,
 *  the same enumerated, html-sink-free subset the NEEDS YOU last words use, so a link stays the
 *  characters it is. What a DOM emulator cannot prove: that the list is laid out as a list inside
 *  the narrow sidebar and a phone, and that a code block scrolls inside the answer instead of
 *  pushing the page sideways.
 */
import { expect, test, type Page } from "@playwright/test";

import { openAsk } from "./askSidebar";
import { mockAskConversations, turnOf } from "./askStream";

const ANSWER = [
  "I can look things up in BattleLab and report back. Specifically:",
  "",
  '- **Find sessions** — search your sessions and missions by topic (e.g. "the auth refactor session").',
  "- **Check session status** — see if a session is working or idle, and view its terminal screen.",
  "- **Missions** — list your missions and see one mission's objectives and progress.",
  "- **Projects and agents** — list your projects and the coding agents available here.",
  "",
  "What I *can't* do: I'm read-only. Run `agent-sessions status` yourself, or see [docs](javascript:alert(1)).",
  "",
  "```",
  "agent-sessions status --all --verbose --format=table --include-archived --since=2026-10-01",
  "```",
].join("\n");

async function mockShell(page: Page) {
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
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
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
  await mockAskConversations(page, [
    {
      id: "ask_" + "d".repeat(32),
      created_at: 1,
      updated_at: 2,
      turns: [turnOf("what can you do?", { answer: ANSWER })],
    },
  ]);
}

test("an answer's bullets, bold, italic and code render as such — and nothing else does", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  const answer = panel.getByTestId("ask-answer");
  await expect(answer).toContainText("I can look things up");

  // The four capabilities are a real list, laid out as one: each item its own line, indented
  // past the paragraph above it.
  const items = answer.locator("ul > li");
  await expect(items).toHaveCount(4);
  await expect(answer.locator("li strong").first()).toHaveText("Find sessions");
  await expect(answer.locator("em")).toHaveText("can't");
  await expect(answer.locator("p code")).toHaveText("agent-sessions status");
  const para = await answer.locator("p").first().boundingBox();
  const first = await items.nth(0).boundingBox();
  const second = await items.nth(1).boundingBox();
  expect(first!.x).toBeGreaterThan(para!.x);
  expect(second!.y).toBeGreaterThanOrEqual(first!.y + first!.height - 1);

  // No markdown punctuation left showing, and a link is NOT in the subset: no anchor, its
  // characters stay literal.
  const text = (await answer.textContent()) ?? "";
  expect(text).not.toContain("**");
  expect(text).not.toContain("- **");
  expect(text).not.toContain("```");
  await expect(answer.locator("a")).toHaveCount(0);
  expect(text).toContain("[docs](javascript:alert(1))");

  // The long code line scrolls inside its block; the page never scrolls sideways.
  const pre = answer.locator("pre");
  await expect(pre).toHaveCount(1);
  expect(await pre.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(
    true,
  );
  const preBox = (await pre.boundingBox())!;
  const panelBox = (await panel.boundingBox())!;
  expect(preBox.x + preBox.width).toBeLessThanOrEqual(
    panelBox.x + panelBox.width + 1,
  );
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <=
        document.documentElement.clientWidth,
    ),
  ).toBe(true);
});
