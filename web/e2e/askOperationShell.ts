/** The HQ sidebar with one operation card in one conversation, for real-browser specs of HQ's
 *  confirm cards (#1559, #1611): every other route the shell touches answers empty. */
import { expect, type Locator, type Page } from "@playwright/test";

import { openAsk, pickConversation } from "./askSidebar";
import { mockAskConversations, turnOf, type FakeAsk } from "./askStream";
import { TOUCH_FLOOR } from "./touchTarget";

export const PID = "a".repeat(32);
export const CID = "ask_" + "a".repeat(32);

export async function shell(
  page: Page,
  proposal: Record<string, unknown>,
): Promise<FakeAsk> {
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
  await page.route("**/api/fs/dirs**", (r) => {
    const path =
      new URL(r.request().url()).searchParams.get("path") || "/home/u";
    const dirs =
      path === "/home/u" ? [{ name: "web", path: "/home/u/web" }] : [];
    return r.fulfill({ json: { path, home: "/home/u", dirs } });
  });
  const t = {
    ...turnOf("archive the docs session", {
      answer: "Confirm it on the card.",
    }),
  };
  t.proposals = [proposal];
  return mockAskConversations(page, [
    { id: CID, created_at: 1, updated_at: 2, turns: [t] },
  ]);
}

/** The decide route: records each body, then the card reads `settle` back. */
export async function decideRoute(
  fake: FakeAsk,
  page: Page,
  settle: Record<string, unknown>,
) {
  const bodies: Record<string, unknown>[] = [];
  await page.route(`**/api/ask/proposals/${PID}/decide`, (r) => {
    const body = r.request().postDataJSON() as Record<string, unknown>;
    bodies.push(body);
    const p = fake.conversations[0].turns[0].proposals![0];
    Object.assign(
      p,
      body.decision === "dismiss"
        ? { state: "dismissed", can_confirm: false, can_dismiss: false }
        : { ...settle, can_confirm: false, can_dismiss: false },
    );
    return r.fulfill({
      status: body.decision === "dismiss" ? 200 : 202,
      json: {
        proposal: {
          state: body.decision === "dismiss" ? "dismissed" : "deciding",
        },
      },
    });
  });
  return bodies;
}

export async function open(page: Page): Promise<Locator> {
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  const c = panel.getByTestId("ask-operation");
  await expect(c).toBeVisible();
  return c;
}

export async function noSidewaysScroll(page: Page) {
  const { scroll, inner } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(inner);
}

export async function minTarget(l: Locator) {
  const box = (await l.boundingBox())!;
  expect(box.height, `${await l.textContent()} height`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
}
