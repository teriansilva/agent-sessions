/** An `assemble` card lists its team as rows (#2130), in a real browser, desktop and mobile.
 *
 *  Driven by the real server-to-card payload (`fixtures/assemble-team.json`, rebuilt and pinned by
 *  `tests/test_assemble_rows_fixture.py`): 14 picks with a kept step, two unassigned steps and the
 *  longest specialist id — far past the old ~120-character sentence. What a DOM emulator cannot
 *  prove: that every row, the last one included, is reachable inside the card by keyboard and by
 *  scrolling while the card's actions stay where they are; that long ids wrap and nothing scrolls
 *  sideways on a phone; that the state words resolve to their tokens and hold AA contrast in both
 *  themes; that a stale card keeps its staged rows with no Confirm; that the 44px floor holds.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  minTarget,
  noSidewaysScroll,
  open,
  PID,
  shell,
} from "./askOperationShell";

type Card = Record<string, unknown> & {
  team?: { state: string; step: string }[];
};
const cards = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "e2e/fixtures/assemble-team.json"),
    "utf8",
  ),
) as Record<"pending" | "stale" | "legacy", Card>;

async function show(
  page: Page,
  which: keyof typeof cards,
  theme: "dark" | "light" = "dark",
) {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await shell(page, { ...cards[which], action_id: PID });
  const c = await open(page);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
  return c;
}

/** WCAG contrast of an element's text against the colour actually painted behind it. */
async function contrast(l: Locator): Promise<number> {
  return l.evaluate((el) => {
    const rgb = (s: string) => {
      const n = (s.match(/[\d.]+/g) || []).map(Number);
      return s.startsWith("color(")
        ? n.slice(0, 3).map((v) => v * 255)
        : n.slice(0, 4);
    };
    let bg: number[] = [];
    for (let e: Element | null = el; e; e = e.parentElement) {
      const c = rgb(getComputedStyle(e).backgroundColor);
      if (c.length >= 3 && (c.length < 4 || c[3] > 0)) {
        bg = c.slice(0, 3);
        break;
      }
    }
    const fg = rgb(getComputedStyle(el).color).slice(0, 3);
    const lum = ([r, g, b]: number[]) => {
      const f = (v: number) => {
        const x = v / 255;
        return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  });
}

/** The colour a token resolves to on this page, as the browser computes it. */
async function token(page: Page, name: string): Promise<string> {
  return page.evaluate((n) => {
    const p = document.createElement("span");
    p.style.color = `var(${n})`;
    document.body.append(p);
    const c = getComputedStyle(p).color;
    p.remove();
    return c;
  }, name);
}

/** Every row's own box holds its content: long ids wrap, nothing is cut or pushed sideways. */
async function noRowOverflow(rows: Locator) {
  const over = await rows.evaluateAll((els) =>
    els
      .filter((e) => e.scrollWidth > e.clientWidth + 1)
      .map((e) => e.textContent),
  );
  expect(over, "rows overflowing their own width").toEqual([]);
}

for (const theme of ["dark", "light"] as const) {
  test(`every pick is a row, the last one reachable by keyboard and by scrolling (${theme})`, async ({
    page,
  }) => {
    const c = await show(page, "pending", theme);
    const team = c.getByTestId("ask-team");
    const rows = team.getByTestId("ask-team-row");
    const all = cards.pending.team!;
    await expect(rows).toHaveCount(all.length);
    await expect(rows.nth(0)).toContainText(all[0].step);
    // The count line comes first: the whole team before any row.
    await expect(team.getByTestId("ask-team-counts")).toHaveText(
      /Team\s*14 steps\s*11 picked\s*1 kept\s*2 unassigned/i,
    );
    // Version and effective model under every pick; a reason under every unassigned step.
    const kept = rows.and(c.locator('[data-state="kept"]'));
    await expect(kept).toHaveCount(1);
    await expect(kept).toContainText(
      "career-archivist-for-the-eu-tenants-backfill-ops",
    );
    await expect(kept).toContainText("v1");
    await expect(kept).toContainText("claude-api · sonnet");
    const open = rows.and(c.locator('[data-state="unassigned"]'));
    await expect(open).toHaveCount(2);
    await expect(open.first()).toContainText("No specialist");
    // The sentence names no picks: the rows replace it.
    await expect(c.getByTestId("ask-operation-text")).not.toContainText("→");
    // The list scrolls INSIDE the card; the card's actions do not move while it does.
    // The scroll region WRAPS the list (the list keeps its own semantics).
    const list = team.getByTestId("ask-team-scroll");
    await expect(team.getByRole("list")).toHaveCount(1);
    const confirm = c.getByTestId("ask-operation-confirm");
    // Where the actions sit IN THE CARD (the page may scroll the focused list into view; the
    // card itself must not grow or reflow as its list scrolls).
    const inCard = async () =>
      (await confirm.boundingBox())!.y - (await c.boundingBox())!.y;
    const before = await inCard();
    const last = rows.last();
    await expect(last).not.toBeInViewport({ ratio: 1 });
    // By keyboard: the list is one focusable region, scrolled with the keys.
    await expect(list).toHaveAttribute("tabindex", "0");
    await expect(list).toHaveAttribute("aria-label", "Team, 14 steps");
    await list.focus();
    await expect(list).toBeFocused();
    await page.keyboard.press("End");
    await expect
      .poll(() =>
        list.evaluate(
          (e) => e.scrollTop + e.clientHeight >= e.scrollHeight - 1,
        ),
      )
      .toBe(true);
    await expect(last).toBeInViewport();
    expect(await inCard()).toBeCloseTo(before, 0);
    // …and by scrolling (wheel over the list), from the top again.
    await list.evaluate((e) => (e.scrollTop = 0));
    await list.hover();
    for (let i = 0; i < 12; i += 1) await page.mouse.wheel(0, 400);
    await expect(last).toBeInViewport();
    // Tokens, contrast, wrapping, the 44px floor.
    const picked = rows.and(c.locator('[data-state="assembled"]')).first();
    const word = picked.locator("span").nth(1);
    expect(await word.evaluate((e) => getComputedStyle(e).color)).toBe(
      await token(page, "--hud-text"),
    );
    const att = open.first().locator("span").nth(1);
    expect(await att.evaluate((e) => getComputedStyle(e).color)).toBe(
      await token(page, "--warn-text"),
    );
    for (const l of [word, att, kept.locator("span").nth(1)])
      expect(
        await contrast(l),
        `${theme} ${await l.textContent()}`,
      ).toBeGreaterThanOrEqual(4.5);
    await noRowOverflow(rows);
    await noSidewaysScroll(page);
    await minTarget(confirm);
    await minTarget(c.getByTestId("ask-operation-dismiss"));
  });
}

test("the scrolling list shows a visible focus ring when reached by keyboard", async ({
  page,
}) => {
  const c = await show(page, "pending");
  const list = c.getByTestId("ask-team-scroll");
  // Arrive by keyboard: :focus-visible is the keyboard's, not a script's.
  await c.getByTestId("ask-operation-text").click();
  for (let i = 0; i < 40; i += 1) {
    if (await list.evaluate((e) => e === document.activeElement)) break;
    await page.keyboard.press("Tab");
  }
  await expect(list).toBeFocused();
  // The card's own inset reticle, not a browser or global default.
  const ring = await list.evaluate((e) => {
    const s = getComputedStyle(e);
    return { style: s.outlineStyle, color: s.outlineColor, offset: s.outlineOffset };
  });
  expect(ring.style).toBe("solid");
  expect(ring.color).toBe(await token(page, "--accent-text"));
  expect(ring.offset).toBe("-3px");
});

test("a stale card keeps its STAGED rows and offers no Confirm", async ({
  page,
}) => {
  const c = await show(page, "stale");
  await expect(c).toHaveAttribute("data-state", "stale");
  const team = c.getByTestId("ask-team");
  await expect(team.getByTestId("ask-team-staged")).toContainText(
    "As staged at plan revision 1.",
  );
  await expect(team.getByTestId("ask-team-counts")).toContainText(
    /Staged team/i,
  );
  const rows = team.getByTestId("ask-team-row");
  await expect(rows).toHaveCount(cards.stale.team!.length);
  for (const [i, r] of cards.stale.team!.entries())
    await expect(rows.nth(i)).toContainText(r.step);
  await expect(c.getByTestId("ask-operation-confirm")).toHaveCount(0);
  await noSidewaysScroll(page);
});

test("a card staged before rows shows its stored sentence and no rows", async ({
  page,
}) => {
  const c = await show(page, "legacy");
  await expect(c.getByTestId("ask-team")).toHaveCount(0);
  await expect(c.getByTestId("ask-operation-text")).toHaveText(
    String(cards.legacy.text),
  );
  await noSidewaysScroll(page);
});

test("a card that ran shows its team plainly: no staged notice on a done card", async ({
  page,
}) => {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), "dark");
  await shell(page, {
    ...cards.pending,
    action_id: PID,
    state: "done",
    summary: "Assembled.",
    can_confirm: false,
    can_dismiss: false,
  });
  const c = await open(page);
  const team = c.getByTestId("ask-team");
  await expect(team.getByTestId("ask-team-row")).toHaveCount(cards.pending.team!.length);
  await expect(team.getByTestId("ask-team-staged")).toHaveCount(0);
  await expect(team.getByTestId("ask-team-counts")).not.toContainText(/Staged/i);
  await expect(team.getByTestId("ask-team-counts")).toHaveText(
    /Team\s*14 steps\s*11 picked\s*1 kept\s*2 unassigned/i,
  );
});
