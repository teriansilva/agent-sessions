import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page } from "@playwright/test";

// The design rules in docs/design.md are enforced here rather than re-asserted in a review
// comment. Every one of these caught a real defect in the #828 mockup: the buttons were 40px, the
// mobile TOC 39px, the card links 15px, and the prose was mono. A rule nothing measures is a rule
// that drifts back.

const PAGES = ["/", "/security/", "/guide/missions", "/reference/"];
// A transform or a fractional line box can read a 44 px target as 43.99997 px; 0.01 px cannot hide
// a real violation, which is at least one device pixel short (same floor as web/e2e/touchTarget.ts).
const TOUCH_FLOOR = 44 - 0.01;
const THEMES = ["dark", "light"] as const;

// VitePress reads its appearance from localStorage before hydrating; setting it in an init script
// means the FIRST paint is already in the theme under test, so nothing is measured mid-transition.
async function open(page: Page, path: string, theme: (typeof THEMES)[number]) {
  await page.addInitScript((t) => {
    localStorage.setItem("vitepress-theme-appearance", t);
  }, theme);
  await page.goto(path);
  await page.waitForLoadState("networkidle");
}

for (const theme of THEMES) {
  test.describe(`${theme} theme`, () => {
    test("the palette bridge marks the document, so tokens.css applies", async ({ page }) => {
      // hud.css imports the APP's palette, which keys off :root / :root[data-theme="light"].
      // VitePress signals with html.dark. If theme/index.ts stops mirroring the two, the page
      // silently renders with the dark palette in light mode — visible, but not obviously a bug.
      await open(page, "/", theme);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      const bg = await page.evaluate(() =>
        getComputedStyle(document.body).backgroundColor,
      );
      expect(bg, "body paints a token background, not a transparent default").not.toBe(
        "rgba(0, 0, 0, 0)",
      );
    });

    for (const path of PAGES) {
      test(`${path} — interactive elements clear the 44px touch floor`, async ({
        page,
      }, testInfo) => {
        test.skip(testInfo.project.name !== "mobile", "44px is a touch rule (design.md §8)");
        await open(page, path, theme);
        const undersized = await page.evaluate((floor) => {
          const out: string[] = [];
          // "Visible" has to mean visible to a FINGER, not merely present in the layout tree.
          // Three things fail that and are correctly exempt: the skip-to-content link (clipped
          // until focused), heading permalinks (opacity 0 — revealed on hover, which a touch
          // device does not have), and anything with no box. Exempting them is not softening the
          // rule: a target nobody can see is not a target, and sizing it up would create an
          // invisible 44px tap zone next to every heading, which is worse than the nit.
          const invisible = (el: HTMLElement) => {
            const s = getComputedStyle(el);
            if (s.visibility === "hidden" || s.display === "none") return true;
            if (parseFloat(s.opacity) === 0) return true;
            // The `visually-hidden` / skip-link pattern: clipped to nothing.
            return s.clipPath === "inset(50%)" || s.clip === "rect(0px, 0px, 0px, 0px)";
          };
          for (const el of document.querySelectorAll<HTMLElement>(
            "a, button, input, summary, [role='button']",
          )) {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue; // not rendered
            if (invisible(el)) continue;
            // Anchors inside a sentence get their hit area from padding, which the bounding box
            // of an inline box does not report; measure their client rects instead.
            const boxes = el.getClientRects();
            const tall = [...boxes].some((b) => b.height >= floor);
            if (!tall && r.height < floor) {
              out.push(`${el.tagName}.${el.className || "-"}:${Math.round(r.height)}`);
            }
          }
          return out;
        }, TOUCH_FLOOR);
        expect(undersized, `elements under 44px on ${path}`).toEqual([]);
      });

      test(`${path} — the page does not scroll horizontally`, async ({ page }) => {
        await open(page, path, theme);
        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        );
        expect(overflow, "a docs page must never scroll sideways").toBeLessThanOrEqual(1);
      });
    }

    test("prose is system sans, headings the display face, code mono (design.md §4)", async ({ page }) => {
      await open(page, "/security/", theme);
      const fonts = await page.evaluate(() => {
        const first = (sel: string) => document.querySelector(sel);
        const family = (el: Element | null) =>
          el ? getComputedStyle(el).fontFamily.toLowerCase() : "";
        return {
          prose: family(first(".vp-doc p")),
          heading: family(first(".vp-doc h2")),
          code: family(first(".vp-doc code")),
        };
      });
      expect(fonts.prose, "body prose is the system sans stack").not.toMatch(/mono/);
      expect(fonts.heading, "section headings are the NIGHTJAR display face").toMatch(/^"?battlelab display/);
      expect(fonts.code, "code is mono").toMatch(/mono/);
    });

    test("focus is a visible accent reticle, not the UA ring (design.md §8)", async ({
      page,
    }) => {
      await open(page, "/security/", theme);
      const link = page.locator(".VPSidebarItem a.link").first();
      await link.focus();
      const outline = await link.evaluate((el) => {
        const s = getComputedStyle(el);
        return { width: s.outlineWidth, style: s.outlineStyle };
      });
      expect(outline.style).toBe("solid");
      expect(parseFloat(outline.width)).toBeGreaterThanOrEqual(2);
    });
  });
}

test("the footer carries the build provenance stamp (#828)", async ({ page }) => {
  await open(page, "/", "dark");
  const footer = page.locator(".VPFooter");
  await expect(footer).toContainText("Built from");
  // The stamp must never invent an ahead-count it cannot justify; "unknown" is a valid state and
  // a bare number with no provenance is not.
  await expect(footer).toContainText(/last publish|matches last publish/);
});

// ---- NIGHTJAR theme (#1597, epic #1531 Phase 12c) ----

for (const path of PAGES) {
  test(`${path} — no sideways scroll at 390 px`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await open(page, path, "dark");
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });
}

test("search opens on Ctrl K and Esc closes it with focus restored", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "a keyboard shortcut");
  await open(page, "/guide/missions", "dark");
  const link = page.locator(".VPSidebarItem a.link").first();
  await link.focus();
  await page.keyboard.press("Control+k");
  const box = page.locator(".VPLocalSearchBox");
  await expect(box).toBeVisible();
  await expect(box.locator("input")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(box).toHaveCount(0);
  await expect(link, "focus returns to where it was").toBeFocused();
});

// A result title that wraps (a long page name on a phone) must not print over itself: the titles
// once ran 16 px text on a 14 px line, so the second line overlapped the first.
test("search result titles have room for a wrapped second line", async ({ page }) => {
  await open(page, "/guide/missions", "dark");
  await page.keyboard.press("Control+k");
  const box = page.locator(".VPLocalSearchBox");
  await box.locator("input").fill("mission");
  await expect(box.locator(".result").first()).toBeVisible();
  const cramped = await box.locator(".result .titles .text").evaluateAll((els) =>
    els
      .map((el) => {
        const s = getComputedStyle(el);
        return [el.textContent?.trim().slice(0, 30), parseFloat(s.lineHeight), parseFloat(s.fontSize)] as const;
      })
      .filter(([, lh, fs]) => !(lh >= fs * 1.2)),
  );
  expect(cramped, "line-height at least 1.2 × the font size").toEqual([]);
});

// Hermes 6787 P1: the search button activated from the keyboard is the opener focus goes back to.
for (const key of ["Enter", "Space"]) {
  test(`search opened with ${key} on the search button gives focus back to it on Esc`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "the search button is an icon in the phone nav");
    await open(page, "/guide/missions", "dark");
    const button = page.locator(".VPNavBarSearch .DocSearch-Button");
    await button.focus();
    await page.keyboard.press(key);
    const box = page.locator(".VPLocalSearchBox");
    await expect(box.locator("input")).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(box).toHaveCount(0);
    await expect(button, "focus returns to the search button").toBeFocused();
  });
}

for (const theme of THEMES) {
  test(`the 404 renders as "Sector not found" (${theme})`, async ({ page }) => {
    await open(page, "/no-such-page-at-all", theme);
    await expect(page.locator(".NotFound .code")).toHaveText("404");
    const home = page.locator(".NotFound .link");
    await expect(home).toBeVisible();
    expect((await home.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  });
}

test("reading pages never move; only the hero and the search modal animate, and not under reduced motion", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await open(page, "/", "dark");
  expect(await page.evaluate(() => document.getAnimations().length), "the home under reduced motion").toBe(0);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await open(page, "/guide/missions", "dark");
  expect(await page.evaluate(() => document.getAnimations().length), "a reading page in full motion").toBe(0);
});

// The capture mode (as web/e2e/appmode-served-pages.spec.ts): with NIGHTJAR_CAPTURE_DIR set, the
// docs site is photographed at 1440 and 390, dark and light, named `<capture id>-<width>-<theme>-1x.png`
// like the app's sweep (web/visual/nightjar). The ids are the ledger's `docs:*` captures.
const CAPTURE_DIR = process.env.NIGHTJAR_CAPTURE_DIR;
test("capture mode: the docs site as the ledger's docs:* captures", async ({ page }, testInfo) => {
  test.skip(!CAPTURE_DIR || testInfo.project.name !== "desktop", "only with NIGHTJAR_CAPTURE_DIR, desktop project");
  testInfo.setTimeout(120_000);
  mkdirSync(CAPTURE_DIR!, { recursive: true });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const shots: [string, string][] = [
    ["docs:home", "/"],
    ["docs:page", "/guide/missions"],
    ["docs:search", "/guide/missions"],
    ["docs:404", "/no-such-page-at-all"],
  ];
  for (const theme of THEMES)
    for (const [id, path] of shots)
      for (const [w, h] of [
        [1440, 960],
        [390, 844],
      ]) {
        await page.setViewportSize({ width: w, height: h });
        await open(page, path, theme);
        if (id === "docs:search") {
          await page.keyboard.press("Control+k");
          const box = page.locator(".VPLocalSearchBox");
          await box.locator("input").fill("mission");
          await expect(box.locator(".result").first()).toBeVisible();
        }
        await page.screenshot({ path: resolve(CAPTURE_DIR!, `${id.replace(/[:/]/g, "_")}-${w}-${theme}-1x.png`) });
      }
});
