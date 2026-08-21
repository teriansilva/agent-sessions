import { test, expect, type Page } from "@playwright/test";

// The design rules in docs/design.md are enforced here rather than re-asserted in a review
// comment. Every one of these caught a real defect in the #828 mockup: the buttons were 40px, the
// mobile TOC 39px, the card links 15px, and the prose was mono. A rule nothing measures is a rule
// that drifts back.

const PAGES = ["/", "/security/", "/guide/pulse", "/reference/"];
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
        const undersized = await page.evaluate(() => {
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
            const tall = [...boxes].some((b) => b.height >= 44);
            if (!tall && r.height < 44) {
              out.push(`${el.tagName}.${el.className || "-"}:${Math.round(r.height)}`);
            }
          }
          return out;
        });
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

    test("prose is system sans and HUD chrome is mono (design.md §4)", async ({ page }) => {
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
      expect(fonts.heading, "section headings are HUD chrome").toMatch(/mono/);
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
