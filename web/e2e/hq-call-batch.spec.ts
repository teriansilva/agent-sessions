/** One yes for a batch of HQ cards on a call (#2389), in a real browser on desktop and mobile.
 *
 *  The server is mocked (`callFixture.ts`): HQ staged twelve renames and the call armed ONE spoken
 *  window for all of them. Pinned: one voice band ("Say “yes” or “do them all”") and no per-card
 *  ordinal; three cards, then "+ 9 more", a keyboard-reachable toggle that keeps focus and shows
 *  every card; each result gets its own stamp, and a card that did not go through stays in view
 *  when folded; 44 px targets and AA text in both themes. */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { CID, FAKE_MIC, connect, mockShell, say } from "./callFixture";
import { TOUCH_FLOOR } from "./touchTarget";

test.use(FAKE_MIC);

const ids = Array.from({ length: 12 }, (_, i) => String(i + 1).padStart(32, "0"));

function rename(id: string, i: number) {
  return {
    action_id: id,
    kind: "operation",
    op: "session.rename",
    risk: "reversible",
    label: "Rename",
    verb: "Rename",
    title: `# Handoff ${i + 1}`,
    text: `Renames it to “Batch title ${i + 1}”.`,
    needs: [],
    state: "pending",
    summary: "",
    reason: "",
    can_confirm: true,
    can_dismiss: true,
  };
}

async function withTwelveRenames(page: Page) {
  await page.route(/\/api\/ask\/conversations\/c{32}$/, (r) =>
    r.fulfill({
      json: {
        id: CID,
        title: "call",
        running: false,
        turns: [
          {
            turn_id: "t1",
            question: "Rename the twelve handoff sessions from their first request.",
            asked_at: 1,
            status: "done",
            answer: "Okay.",
            detail: null,
            code: null,
            matches: [],
            mission_matches: [],
            tools: [],
            ask: null,
            contact: "asia-woman",
            proposals: ids.map(rename),
          },
        ],
      },
    }),
  );
}

/** WCAG contrast of an element's text against the first opaque background under it. */
async function contrast(l: Locator): Promise<number> {
  return l.evaluate((el) => {
    const rgb = (s: string) => {
      const n = (s.match(/[\d.]+/g) || []).map(Number);
      return s.startsWith("color(") ? n.slice(0, 3).map((v) => v * 255) : n.slice(0, 3);
    };
    const opaque = (s: string) => {
      const n = (s.match(/[\d.]+/g) || []).map(Number);
      return n.length === 3 || (n.length >= 4 && n[3] === 1);
    };
    let g: Element | null = el;
    while (g && !opaque(getComputedStyle(g).backgroundColor)) g = g.parentElement;
    const bg = g ? getComputedStyle(g).backgroundColor : "rgb(255, 255, 255)";
    const lum = ([r, gr, b]: number[]) => {
      const f = (v: number) => {
        const c = v / 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * f(r) + 0.7152 * f(gr) + 0.0722 * f(b);
    };
    const [a, b] = [lum(rgb(getComputedStyle(el).color)), lum(rgb(bg))].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  });
}

for (const theme of ["dark", "light"] as const) {
  test(`one band for a batch, + N more reaches every card, 44 px and AA (${theme})`, async ({ page }, info) => {
    await page.emulateMedia({ colorScheme: theme });
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    const srv = await mockShell(page);
    await withTwelveRenames(page);
    const panel = await connect(page, srv);
    srv.connected[0] = true;
    say(srv, { t: "connected" });
    await expect(panel).toHaveAttribute("data-phase", "connected");
    say(srv, { t: "turn", turn: { turn_id: "t1", status: "done" } });
    say(srv, { t: "challenge", cards: ids, expires_s: 20, batch: true });

    // ONE band for all twelve, and no card asks for its ordinal.
    const band = panel.getByTestId("call-batch-voice");
    await expect(band.getByTestId("call-batch-say")).toHaveText(/^Say “yes” or “do them all” · (20|19|18) s$/);
    await expect(band).toContainText("Or name one. Tapping a card does only that card.");
    await expect(panel.getByTestId("call-card-say")).toHaveCount(0);
    for (const text of [band.getByTestId("call-batch-say"), band.getByText(/^Or name one/)])
      expect(await contrast(text), theme).toBeGreaterThanOrEqual(4.5);

    // Three cards, then "+ 9 more": a real button, at the floor, reached by the keyboard.
    const cards = panel.getByTestId("call-card");
    await expect(cards).toHaveCount(3);
    const more = panel.getByTestId("call-cards-more");
    await expect(more).toHaveText("+ 9 more");
    await expect(more).toHaveAttribute("aria-expanded", "false");
    expect((await more.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(await contrast(more)).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: info.outputPath(`call-batch-armed-${theme}.png`) });
    await more.focus();
    await expect(more).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(cards).toHaveCount(12);
    await expect(more).toHaveAttribute("aria-expanded", "true");
    await expect(more).toBeFocused(); // focus stays where it was
    await expect(more).toHaveText("Show fewer");
    for (const id of ids) {
      const card = panel.locator(`[data-testid="call-card"][data-card="${id}"]`);
      await card.scrollIntoViewIfNeeded();
      await expect(card).toBeVisible();
      for (const b of await card.getByRole("button").all()) {
        const box = await b.boundingBox();
        if (box) expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      }
    }
    await more.click(); // fold again
    await expect(cards).toHaveCount(3);

    // The yes came: each card gets its own stamp; one that failed stays in view when folded.
    say(srv, { t: "challenge", cards: [] });
    for (const id of ids.slice(0, 11).filter((_, i) => i !== 9))
      say(srv, { t: "settled", card: id, ok: true, status: 200 });
    say(srv, { t: "settled", card: ids[9], ok: false, status: 409 });
    say(srv, { t: "settled", card: ids[11], ok: false, status: 0, unknown: true });
    await expect(panel.getByTestId("call-batch-voice")).toHaveCount(0);
    const failed = panel.locator(`[data-testid="call-card"][data-card="${ids[9]}"]`);
    const unknown = panel.locator(`[data-testid="call-card"][data-card="${ids[11]}"]`);
    await expect(failed.getByTestId("call-card-voice")).toContainText("Changed — not sent");
    await expect(unknown.getByTestId("call-card-voice")).toContainText("Can't tell — not retried");
    await expect(panel.locator(`[data-testid="call-card"][data-card="${ids[0]}"]`)).toContainText(
      "✓ Done by voice",
    );
    await expect(cards).toHaveCount(5);
    await expect(more).toHaveText("+ 7 more");
    for (const stamp of [failed, unknown].map((c) => c.getByTestId("call-card-voice").locator(".nj-stamp")))
      expect(await contrast(stamp), theme).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: info.outputPath(`call-batch-settled-${theme}.png`) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
