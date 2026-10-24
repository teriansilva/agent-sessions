/** The headers in NIGHTJAR v5 (#1589), in a real browser.
 *
 *  - THE TOP BAR at every width: each section is a PLATE (`nj-plate`), numbered 01–04 from the one
 *    sections array above 960px (the number is `aria-hidden`: the link's name stays its label); a
 *    section with a sub-menu is ONE plate holding its link and its chevron; at ≤800px the label is
 *    CLIPPED (in the DOM, never removed) and the icon is the face; BETA is a tag under the icon, never
 *    over it; no entry runs under the corner, nothing scrolls sideways, and on a touch screen every
 *    control is a finger's target. A plate's painted corners stay its own hit area (#946).
 *  - THE BRAND KIT'S LOGO: the Chevron Plate mark at every width (≥ the kit's 16px minimum), the
 *    outlined wordmark above 800px, and only the pair for the theme on screen.
 *  - THE SESSION HEAD on a touch tablet (801–1180px): the actions fold into the one Actions menu, so
 *    the facts keep their width — terminal and structured panes alike.
 */
import { expect, test, type Browser, type Page } from "@playwright/test";

import { ID, mockStructuredGuidance } from "./structuredGuidanceFixture";
import { setupBench } from "./terminal/harness";
import { TOUCH_FLOOR } from "./touchTarget";

const ENGINE = "claude";
const UUID = "ffffffff-1111-2222-3333-444444444444";
const TITLE = "Tidy the compose bar";

async function device(browser: Browser, width: number, touch: boolean, theme: "dark" | "light" = "dark") {
  const ctx = await browser.newContext({
    viewport: { width, height: 800 },
    hasTouch: touch,
    isMobile: touch && width <= 800,
  });
  await ctx.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  return { ctx, page: await ctx.newPage() };
}

async function openTerminalSession(page: Page) {
  await setupBench(page, { sessions: [{ engine: ENGINE, uuid: UUID, title: TITLE }] });
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        sessions: [
          {
            id: `${ENGINE}:${UUID}`,
            engine: ENGINE,
            uuid: UUID,
            short_uuid: "ffffffff",
            cwd: "/home/u/proj",
            project: { kind: "folder", id: "/home/u/proj", name: "/home/u/proj" },
            last_mtime: 1_700_000_000,
            first_user_message: "",
            title: TITLE,
            sticky: false,
            archived: false,
            mission: null,
          },
        ],
        next_offset: null,
        total: 1,
        facets: { projects: [], engines: [ENGINE], missions: [], no_mission: 1 },
      },
    }),
  );
  await page.goto(`/s/${ENGINE}/${UUID}`);
  await expect(page.locator(".hud-topbar")).toBeVisible();
}

/** Each case builds its own device (viewport, pointer, theme), so it runs once — in the desktop
 *  project; the mobile project's Pixel would only repeat it. */
function onceOnDesktop() {
  test.skip(test.info().project.name !== "desktop", "each case sets its own device");
}

/** A real press at one point: a tap on a touch device, a click with a mouse. */
async function pressAt(page: Page, touch: boolean, x: number, y: number) {
  if (touch) await page.touchscreen.tap(x, y);
  else await page.mouse.click(x, y);
}

/** Every action a session head offers, by accessible name: its chips plus whatever folded into a
 *  menu (the "…" overflow, or the compact Actions menu). Order-free. */
async function headActionNames(page: Page, head: ReturnType<Page["locator"]>): Promise<string[]> {
  const name = (els: Element[]) =>
    els.map((e) => (e.getAttribute("aria-label") || e.textContent || "").trim()).filter(Boolean);
  const names = await head.locator("[data-head-action]").evaluateAll(name);
  const fold = head.getByRole("button", { name: /^(More session actions|Actions for this session)$/ });
  if (await fold.count()) {
    const label = (await fold.getAttribute("aria-label"))!;
    await fold.click();
    const menu = page.getByRole("menu", { name: label });
    await expect(menu).toBeVisible();
    names.push(...(await menu.getByRole("menuitem").evaluateAll(name)));
    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
  }
  return names.sort();
}

const WIDTHS = [
  { width: 1440, touch: false },
  { width: 1100, touch: true },
  { width: 834, touch: true },
  { width: 412, touch: true },
  { width: 390, touch: true },
  { width: 320, touch: true },
] as const;
const LABELS = ["Dashboard", "Missions", "Sessions", "Library"];

test.describe("the top bar (#1589)", () => {

  for (const { width, touch } of WIDTHS) {
    test(`v5 at ${width}px${touch ? " (touch)" : ""}: numbered plates, chevrons inside, no collisions`, async ({
      browser,
    }) => {
      onceOnDesktop();
      const { ctx, page } = await device(browser, width, touch);
      await openTerminalSession(page);
      const nav = page.getByRole("navigation", { name: "Main sections" });
      const corner = (await page.locator(".hud-topbar-actions").boundingBox())!;

      for (const [i, name] of LABELS.entries()) {
        const link = nav.getByRole("link", { name, exact: true }); // the name is the label, not "01 …"
        await expect(link).toHaveCount(1);
        // A plate: the link, or the split group that holds it.
        const plated = await link.evaluate(
          (el) => el.classList.contains("nj-plate") || !!el.parentElement?.classList.contains("nj-plate"),
        );
        expect(plated, `${name} is a plate at ${width}`).toBe(true);
        const ix = link.locator(".section-nav-ix");
        await expect(ix).toHaveAttribute("aria-hidden", "true");
        await expect(ix).toHaveText(String(i + 1).padStart(2, "0"));
        const label = link.locator(".section-nav-label");
        await expect(label).toHaveText(name); // always in the DOM
        const lb = (await label.boundingBox())!;
        if (width > 960) {
          await expect(ix).toBeVisible();
          expect(lb.width, `${name} label shown at ${width}`).toBeGreaterThan(20);
        } else if (width > 800) {
          // The compact-desktop band (801–960px, #1589): plates and labels, no numerals.
          await expect(ix).toBeHidden();
          expect(lb.width, `${name} label shown at ${width}`).toBeGreaterThan(20);
        } else {
          // CLIPPED, not removed (shell-sections invariant), and the icon is the face.
          expect(lb.width, `${name} label clipped at ${width}`).toBeLessThanOrEqual(1);
          await expect(link.locator("svg").first()).toBeVisible();
        }
        const b = (await link.boundingBox())!;
        expect(b.x + b.width, `${name} clear of the corner at ${width}`).toBeLessThanOrEqual(corner.x + 0.5);
        if (touch) expect(b.height, `${name} touch target at ${width}`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
        // The painted corners are the link's own hit area (#946): a plate paints its chamfer.
        const corners = await link.evaluate((el) => {
          const r = el.getBoundingClientRect();
          const at = (x: number, y: number) => {
            const hit = document.elementFromPoint(x, y);
            return !!hit && (hit === el || el.contains(hit));
          };
          return [at(r.left + 1, r.top + 1), at(r.left + 1, r.bottom - 1)];
        });
        expect(corners, `${name} corners at ${width}`).toEqual([true, true]);
      }

      for (const name of ["Sessions", "Library"]) {
        const chevron = nav.getByRole("button", { name: `${name} menu` });
        const group = chevron.locator("xpath=ancestor::*[contains(@class,'section-nav-split')][1]");
        await expect(group).toHaveClass(/nj-plate/);
        const cb = (await chevron.boundingBox())!;
        const gb = (await group.boundingBox())!;
        // INSIDE the tab's plate, never a separate box.
        expect(cb.x).toBeGreaterThanOrEqual(gb.x - 0.5);
        expect(cb.x + cb.width).toBeLessThanOrEqual(gb.x + gb.width + 0.5);
        if (touch) expect(cb.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
        // A REAL PRESS at each of the chevron's painted corners opens its menu (Hermes 6779): the
        // plate paints the chamfer, so the corner is still the chevron's.
        const panel = page.getByTestId(`section-menu-${name.toLowerCase()}-panel`);
        for (const [x, y] of [
          [cb.x + cb.width - 2, cb.y + 2],
          [cb.x + cb.width - 2, cb.y + cb.height - 2],
        ]) {
          await pressAt(page, touch, x, y);
          await expect(panel, `${name} menu from its chevron's corner at ${width}`).toBeVisible();
          await expect(chevron).toHaveAttribute("aria-expanded", "true");
          // Keyboard and ARIA unchanged: Escape closes it and gives focus back to the chevron.
          await page.keyboard.press("Escape");
          await expect(panel).toHaveCount(0);
          await expect(chevron).toHaveAttribute("aria-expanded", "false");
          await expect(chevron).toBeFocused();
        }
      }

      // A REAL PRESS at the split LINKS' painted corners navigates (Hermes 6779): Library from the
      // session page, then Sessions back to the session — top-left and bottom-left corner each.
      for (const corner of ["top", "bottom"] as const) {
        for (const [name, to] of [
          ["Library", /\/templates|\/library/],
          ["Sessions", new RegExp(`/s/${ENGINE}/${UUID}`)],
        ] as const) {
          const lb = (await nav.getByRole("link", { name, exact: true }).boundingBox())!;
          await pressAt(page, touch, lb.x + 2, corner === "top" ? lb.y + 2 : lb.y + lb.height - 2);
          await expect(page, `${name} from its ${corner} corner at ${width}`).toHaveURL(to);
          // The URL moves before the route commits, and the session route adds the list toggle
          // (Library has none, #1129), which shifts every tab 48px: measure the next link only
          // once this route's top bar is the one on screen.
          await expect(page.locator("header .navToggle")).toHaveCount(name === "Sessions" ? 1 : 0);
        }
      }

      if (width <= 800) {
        // BETA is a tag UNDER the icon, never a superscript over it.
        const missions = nav.getByRole("link", { name: "Missions", exact: true });
        const icon = (await missions.locator("svg").first().boundingBox())!;
        // The narrow-width tag is the letter β (#1589 decision), decoration: the name stays Missions.
        await expect(missions.locator(".section-nav-beta")).toHaveAttribute("aria-hidden", "true");
        await expect(missions.locator(".section-nav-beta")).toHaveText("Betaβ");
        const tag = (await missions.locator(".section-nav-beta").boundingBox())!;
        expect(tag.y, "BETA below the icon").toBeGreaterThanOrEqual(icon.y + icon.height - 1);
      }

      if (touch) {
        for (const id of ["ask-toggle"]) {
          const c = (await page.getByTestId(id).boundingBox())!;
          expect(c.height, `${id} at ${width}`).toBeGreaterThanOrEqual(width <= 800 ? TOUCH_FLOOR : 30);
        }
      }

      const scroll = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(scroll, `no sideways scroll at ${width}`).toBeLessThanOrEqual(0);
      await ctx.close();
    });
  }

  test("the list toggle sits after the logo, a corner-tile plate lit while its list is open", async ({
    browser,
  }) => {
    onceOnDesktop();
    for (const width of [1440, 834, 390, 320]) {
      const touch = width <= 1180;
      const { ctx, page } = await device(browser, width, touch);
      await openTerminalSession(page);
      const toggle = page.locator(".hud-topbar > .navToggle");
      await expect(toggle).toHaveCount(1);
      const brand = (await page.locator(".hud-brand").boundingBox())!;
      const tb = (await toggle.boundingBox())!;
      const firstSection = (await page.locator(".section-nav a").first().boundingBox())!;
      // Operator, 2026-10-24: the brand leads the bar; the toggle follows it, before the sections.
      expect(tb.x, `toggle after the logo at ${width}`).toBeGreaterThanOrEqual(brand.x + brand.width);
      expect(tb.x + tb.width, `toggle before the sections at ${width}`).toBeLessThanOrEqual(firstSection.x + 0.5);
      // The corner tiles' plate, at their size: HQ's trigger is the reference tile. On a touch
      // tablet above 800px the box is a 44px hit square and the plate is painted inside it, so the
      // PAINTED tile (the box less its ::before inset) is what matches HQ's.
      await expect(toggle).toHaveClass(/\bnj-plate\b/);
      const ask = (await page.getByTestId("ask-toggle").boundingBox())!;
      const inset = await toggle.evaluate((el) => {
        const b = getComputedStyle(el, "::before");
        return { x: parseFloat(b.left) + parseFloat(b.right), y: parseFloat(b.top) + parseFloat(b.bottom) };
      });
      expect(Math.abs(tb.width - inset.x - ask.width), `toggle plate width = HQ's at ${width}`).toBeLessThanOrEqual(0.5);
      if (!touch || width <= 800) {
        expect(Math.abs(tb.height - inset.y - ask.height), `toggle plate height = HQ's at ${width}`).toBeLessThanOrEqual(0.5);
      }
      // A finger's target wherever touch is the pointer, tablets above 800px included (Hermes 6911,
      // 6936): the box itself is the hit area (the plate's ::before/::after are paint), so it must be
      // 44px in BOTH dimensions where there is room (the phone row's tiles share the width, and keep
      // the 44px height), and a tap 1px inside each of its four edges must land on the toggle.
      if (touch) {
        expect(tb.height, `toggle touch target height at ${width}`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
        if (width > 800) expect(tb.width, `toggle touch target width at ${width}`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
        const cx = tb.x + tb.width / 2;
        const cy = tb.y + tb.height / 2;
        const points = [
          [cx, tb.y + 1],
          [cx, tb.y + tb.height - 1],
          [tb.x + 1, cy],
          [tb.x + tb.width - 1, cy],
        ];
        const hits = await page.evaluate(
          (pts) => pts.map(([x, y]) => !!document.elementFromPoint(x, y)?.closest(".navToggle")),
          points,
        );
        expect(hits, `taps 1px inside each edge of the toggle hit it at ${width}`).toEqual([true, true, true, true]);
      }
      // Lit while its list is open, as HQ is while its sidebar is: the edge is the chrome's --hud.
      const edge = () => toggle.evaluate((el) => getComputedStyle(el).getPropertyValue("--nj-edge").trim());
      const hud = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--hud").trim());
      const open = (await toggle.getAttribute("aria-expanded")) === "true";
      if (open) expect(await edge(), `lit while open at ${width}`).toBe(hud);
      else expect(await edge(), `unlit while closed at ${width}`).not.toBe(hud);
      const scroll = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(scroll, `no sideways scroll at ${width}`).toBeLessThanOrEqual(0);
      await ctx.close();
    }
  });

  for (const theme of ["dark", "light"] as const) {
    test(`the brand kit's logo, ${theme}: the theme's pair, the mark at every width, the wordmark above 800px`, async ({
      browser,
    }) => {
      onceOnDesktop();
      for (const width of [1440, 834, 390, 320]) {
        const { ctx, page } = await device(browser, width, width <= 1180, theme);
        await openTerminalSession(page);
        const brand = page.locator(".hud-brand");
        await expect(brand).toContainText("BattleLab"); // its name, for assistive tech
        // ONE mark on screen, and the theme's: the bundled kit files (web/src/brand, #1590) may be
        // inlined as data URIs, so read the image itself — the dark plate's fill is #0E1015.
        const shown = brand.locator(".brand-mark:visible");
        await expect(shown).toHaveCount(1);
        const svg = await shown.evaluate(async (img) => (await fetch((img as HTMLImageElement).src)).text());
        expect(/#0E1015/i.test(svg), `the ${theme} theme's mark`).toBe(theme === "light");
        const mb = (await shown.boundingBox())!;
        expect(mb.width, `mark at ${width}`).toBeGreaterThanOrEqual(16);
        expect(mb.width).toBe(mb.height); // never stretched
        const word = brand.locator("[data-brand-wordmark] img:visible");
        if (width > 800) {
          await expect(word).toHaveCount(1);
          // On dark the word is white + amber; on light it is all black (#07080A).
          const wsvg = await word.evaluate(async (img) => (await fetch((img as HTMLImageElement).src)).text());
          expect(/#07080A/i.test(wsvg), `the ${theme} theme's wordmark`).toBe(theme === "light");
          const wb = (await word.boundingBox())!;
          expect(Math.abs(wb.width / wb.height - 284 / 32)).toBeLessThan(0.1); // the kit's ratio
        } else {
          await expect(word).toHaveCount(0);
        }
        await ctx.close();
      }
    });
  }
});

/** THE session head: the `panelHead` that holds the session's actions (chips or the Actions trigger).
 *  A bare `[class*="panelHead"]` can match another head first — in CI it read the operator's "CO". */
function sessionHead(page: Page, scope?: ReturnType<Page["locator"]>) {
  return (scope ?? page)
    .locator('[class*="panelHead"]')
    .filter({ has: page.locator('[data-head-action], [data-testid="head-actions-menu"]') })
    .first();
}

/** What a session head's facts say, and whether any fact is cut short (an element whose text
 *  overflows its box — the ellipsis that made the project read "o…"). */
async function factsOf(head: ReturnType<Page["locator"]>) {
  const facts = head.locator('[class*="headLeft"]');
  const text = (await facts.innerText()).replace(/\s+/g, " ").trim();
  const cut = await facts.evaluate((el) =>
    [el, ...el.querySelectorAll("*")].filter((e) => e.scrollWidth > e.clientWidth + 1).length,
  );
  return { facts, text, cut };
}

/** The facts are READABLE (Hermes 6779): nothing in them is cut short, they hold most of the head,
 *  and they say either a real line or everything the desktop says for the same session. */
async function expectReadableFacts(head: ReturnType<Page["locator"]>, width: number, desktopText: string) {
  const hb = (await head.boundingBox())!;
  const { facts, text, cut } = await factsOf(head);
  expect(cut, `no fact cut short at ${width}: "${text}"`).toBe(0);
  expect((await facts.boundingBox())!.width, `facts' width at ${width}`).toBeGreaterThan(hb.width * 0.6);
  if (text.replace(/[…\s·]/g, "").length <= 6) expect(text, `facts at ${width}`).toBe(desktopText);
}

test.describe("the session head on a touch tablet (#1589)", () => {

  for (const width of [834, 1100]) {
    test(`terminal pane at ${width}px touch: the facts keep the width, the actions are one Actions menu`, async ({
      browser,
    }) => {
      onceOnDesktop();
      const { ctx, page } = await device(browser, width, true);
      await openTerminalSession(page);
      const head = sessionHead(page);
      const trigger = head.getByRole("button", { name: "Actions for this session" });
      await expect(trigger).toBeVisible();
      expect((await trigger.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      // No 44px icon squares left on the bar.
      for (const name of [/open session brief/i, /hand off/i, /bigger terminal text/i]) {
        await expect(head.getByRole("button", { name })).toHaveCount(0);
      }
      // The facts take the width and read in full; EVERY action is reachable — the Actions menu holds
      // exactly what a fine-pointer desktop shows for the same session.
      const desk = await device(browser, 1800, false);
      await openTerminalSession(desk.page);
      const deskHead = sessionHead(desk.page);
      await expect(deskHead.locator("[data-head-action]").first()).toBeVisible();
      const deskFacts = (await factsOf(deskHead)).text;
      const full = await headActionNames(desk.page, deskHead);
      await desk.ctx.close();
      await expectReadableFacts(head, width, deskFacts);
      const tablet = await headActionNames(page, head);
      await ctx.close();
      expect(full.length).toBeGreaterThan(3);
      expect(tablet, `terminal actions at ${width}`).toEqual(full);
    });

    test(`structured pane at ${width}px touch: the same one Actions menu`, async ({ browser }) => {

      onceOnDesktop();
      const { ctx, page } = await device(browser, width, true);
      await mockStructuredGuidance(page, "codex-api", "steer");
      await page.goto(`/s/codex-api/${ID}`);
      const head = sessionHead(page);
      const trigger = head.getByRole("button", { name: "Actions for this session" });
      await expect(trigger).toBeVisible();
      expect((await trigger.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      await expect(head.locator("[data-head-action]")).toHaveCount(0); // no icon squares left
      // Mirrors the terminal case (Hermes 6779): readable facts, and the COMPLETE action set.
      const desk = await device(browser, 1800, false);
      await mockStructuredGuidance(desk.page, "codex-api", "steer");
      await desk.page.goto(`/s/codex-api/${ID}`);
      const deskHead = sessionHead(desk.page);
      await expect(deskHead.locator("[data-head-action]").first()).toBeVisible();
      const deskFacts = (await factsOf(deskHead)).text;
      const full = await headActionNames(desk.page, deskHead);
      await desk.ctx.close();
      await expectReadableFacts(head, width, deskFacts);
      const tablet = await headActionNames(page, head);
      await ctx.close();
      expect(full.length).toBeGreaterThan(1);
      expect(tablet, `structured actions at ${width}`).toEqual(full);
    });
  }

  test("a fine pointer at 1100px keeps the labelled chips (desktop unchanged)", async ({ browser }) => {

    onceOnDesktop();
    const { ctx, page } = await device(browser, 1100, false);
    await openTerminalSession(page);
    const head = sessionHead(page);
    await expect(head.getByRole("button", { name: "Actions for this session" })).toHaveCount(0);
    await ctx.close();
  });
});

/** THE PHONE ROW (≤800px): every control in the bar is ONE tile — the four sections (a sub-menu's
 *  chevron rides beside its link), HQ, the bell and the operator — the same width and the same
 *  44px height; the operator's initials ARE that tile, not a smaller box inside it; and the footer
 *  is ONE line with no version. Before this, at 412px the sections were 28px, HQ and the bell 32px,
 *  the operator a 34px plate around a 32px square, and the footer wrapped the version to a second
 *  line under the telemetry. */
test.describe("the phone row: one tile, one footer line", () => {
  // The widest the strip can get: three-digit CPU and RAM, and an estimated CTX in its longest
  // form ("≈9.99M"). At 320px this once pushed the emptied host/version wrapper onto a second row.
  const TELEMETRY = {
    cpu_pct: 100,
    mem: { used: 64e9, total: 64e9, pct: 100 },
    hq: { ctx_tokens: 9_990_000, ctx_estimated: true, today_tokens: 99_900_000, today_estimated: true, at: 1 },
    as_of: 1,
  };

  for (const width of [320, 360, 380, 381, 412, 440, 441, 600, 800]) {
    test(`${width}px: one tile size, the initials fill theirs, a one-line footer without the version`, async ({
      browser,
    }) => {
      onceOnDesktop();
      const { ctx, page } = await device(browser, width, true);
      await page.route(/\/api\/telemetry/, (r) => r.fulfill({ json: TELEMETRY }));
      await openTerminalSession(page);
      await expect(page.getByTestId("footer-telemetry")).toHaveAttribute("data-state", "ok");
      await expect(page.getByTestId("tm-ctx")).toHaveText("≈9.99M");

      const boxes = async (sel: string) =>
        page.locator(sel).evaluateAll((els) =>
          els.map((e) => {
            const r = e.getBoundingClientRect();
            return { x: r.x, w: r.width, h: r.height };
          }),
        );
      const tiles = [
        ...(await boxes(".section-nav > a.nj-plate, .section-nav-split > a")),
        ...(await boxes(".hud-topbar-actions .nj-plate")),
      ];
      expect(tiles, `seven tiles at ${width}`).toHaveLength(7);
      for (const t of tiles) {
        expect(t.w, `every tile one width at ${width}`).toBeCloseTo(tiles[0].w, 0);
        expect(t.h, `every tile 44px tall at ${width}`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
        expect(t.h).toBeCloseTo(tiles[0].h, 0);
      }
      const chevrons = await boxes(".section-nav-chevron");
      expect(chevrons).toHaveLength(2);
      expect(chevrons[1].w).toBeCloseTo(chevrons[0].w, 0);

      // The operator's initials fill its plate: one box, not a square inside a frame.
      const op = page.locator(".operator-slot .nj-plate");
      const ob = (await op.boundingBox())!;
      const sb = (await op.locator(':scope > span[aria-hidden="true"]').first().boundingBox())!;
      expect(Math.abs(sb.width - ob.width), `initials fill the tile at ${width}`).toBeLessThanOrEqual(2);
      expect(Math.abs(sb.height - ob.height), `initials fill the tile at ${width}`).toBeLessThanOrEqual(2);

      // Nothing overlaps along the row.
      const row = [...tiles, ...chevrons].sort((a, b) => a.x - b.x);
      for (let i = 1; i < row.length; i++) {
        expect(row[i].x, `no overlap at ${width}`).toBeGreaterThanOrEqual(row[i - 1].x + row[i - 1].w - 0.5);
      }

      // The footer: one 28px line, the telemetry readable to its end, and no version.
      const bar = (await page.locator(".hud-classbar").boundingBox())!;
      expect(bar.height, `one footer line at ${width}`).toBeLessThanOrEqual(28.5);
      await expect(page.locator(".hud-version")).toBeHidden();
      // The strip clips its overflow, so the last value must end inside the strip, not just the page.
      const strip = (await page.getByTestId("footer-telemetry").boundingBox())!;
      const ctxv = (await page.getByTestId("tm-ctx").boundingBox())!;
      expect(ctxv.x + ctxv.width, `HQ CTX not clipped at ${width}`).toBeLessThanOrEqual(strip.x + strip.width + 0.5);
      await ctx.close();
    });
  }

  test("a desktop footer still names the version", async ({ browser }) => {
    onceOnDesktop();
    const { ctx, page } = await device(browser, 1280, false);
    await openTerminalSession(page);
    await expect(page.locator(".hud-version")).toBeVisible();
    await ctx.close();
  });
});
