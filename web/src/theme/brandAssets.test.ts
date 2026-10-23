/** The BattleLab logo ships from ONE source (#1590): the brand kit's SVGs in web/src/brand, pinned
 *  by sha256. Every derived copy and inline slot equals what the generator makes from them
 *  (run `node scripts/render-brand-assets.mjs` after changing a source), the shipped SVGs carry
 *  nothing active, the platform PNGs have the sizes the manifest declares, and the old marks are
 *  gone from every surface this issue owns. */
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";

import { BRAND, RASTERS, REPO, SLOTTED, brandFiles, fillSlots, slotNames } from "../../scripts/brand.mjs";

const read = (rel: string) => readFileSync(resolve(REPO, rel), "utf8");

/** The kit's files, verbatim (sha256, first 16 hex). A change here is a brand change: re-pin it on
 *  purpose, with the kit revision it came from. */
const PINS: Record<string, string> = {
  "glyph-amber.svg": "3de6be1473f15079",
  "logo-horizontal-notag-on-dark.svg": "d7234499cf8f5324",
  "logo-horizontal-notag-on-light.svg": "262b30774d3b0b71",
  "logo-horizontal-on-dark.svg": "251b4ed07b55dec0",
  "logo-horizontal-on-light.svg": "ebe2eb73ed42be34",
  "mark-amber.svg": "dcc09b6fd329f423",
  "mark-dark.svg": "ec70c53d3eeeb0b7",
  "wordmark-on-dark.svg": "4351b2ee01767d1b",
  "wordmark-on-light.svg": "e6569d444d82b50f",
};

describe("the brand kit is the one source", () => {
  test("web/src/brand holds exactly the pinned kit files, unchanged", () => {
    const svgs = readdirSync(BRAND).filter((f) => f.endsWith(".svg")).sort();
    expect(svgs).toEqual(Object.keys(PINS).sort());
    for (const f of svgs) {
      const sha = createHash("sha256").update(readFileSync(resolve(BRAND, f))).digest("hex").slice(0, 16);
      expect(sha, f).toBe(PINS[f]);
    }
  });

  test("every derived file equals the generator's output", () => {
    for (const [rel, text] of Object.entries(brandFiles())) expect(read(rel), rel).toBe(text);
  });

  test("every inline slot is filled from the source, and every slotted page has one", () => {
    for (const rel of SLOTTED) {
      const text = read(rel);
      expect(slotNames(text).length, `${rel} carries a brand slot`).toBeGreaterThan(0);
      expect(fillSlots(text), `${rel}: run node scripts/render-brand-assets.mjs`).toBe(text);
    }
  });

  test("a shipped logo SVG carries nothing active and no text", () => {
    const svgs: [string, string][] = [
      ...readdirSync(BRAND)
        .filter((f) => f.endsWith(".svg"))
        .map((f) => [f, readFileSync(resolve(BRAND, f), "utf8")] as [string, string]),
      ...Object.entries(brandFiles()),
    ];
    for (const [name, svg] of svgs) {
      expect(svg, name).not.toMatch(/<script|foreignObject|<text|<style|\bon[a-z]+=|href=|<image|<!--/i);
    }
  });

  test("the platform PNGs have the sizes the manifest and pages declare", () => {
    for (const [, out, w, h] of RASTERS) {
      const png = readFileSync(resolve(REPO, out));
      expect(png.subarray(1, 4).toString("latin1"), out).toBe("PNG");
      expect([png.readUInt32BE(16), png.readUInt32BE(20)], out).toEqual([w, h]);
    }
  });
});

describe("every page links the generated files, and they exist", () => {
  const exists = (rel: string) => {
    try {
      readFileSync(resolve(REPO, rel));
      return true;
    } catch {
      return false;
    }
  };
  // [page, the markup it must carry, the repo file that markup serves]
  const LINKS: [string, string, string][] = [
    ["web/index.html", '<link rel="icon" type="image/svg+xml" href="/favicon.svg" />', "web/public/favicon.svg"],
    ["web/index.html", '<link rel="apple-touch-icon" href="/apple-touch-icon.png" />', "web/public/apple-touch-icon.png"],
    ["web/index.html", '<meta property="og:image" content="/og-image.png" />', "web/public/og-image.png"],
    ["web/index.html", '<meta name="twitter:image" content="/og-image.png" />', "web/public/og-image.png"],
    ["web/share.html", '<link rel="icon" type="image/svg+xml" href="/favicon.svg" />', "web/public/favicon.svg"],
    ["landing/index.html", '<link rel="icon" type="image/svg+xml" href="/favicon.svg" />', "landing/favicon.svg"],
    ["landing/legal.html", '<link rel="icon" type="image/svg+xml" href="/favicon.svg" />', "landing/favicon.svg"],
    ["landing/privacy.html", '<link rel="icon" type="image/svg+xml" href="/favicon.svg" />', "landing/favicon.svg"],
    ["landing/index.html", '<meta property="og:image" content="https://battlelabos.com/og.png" />', "landing/og.png"],
    ["landing/index.html", '<meta name="twitter:image" content="https://battlelabos.com/og.png" />', "landing/og.png"],
    ["docs/site/.vitepress/config.ts", 'href: "/favicon.svg"', "docs/site/public/favicon.svg"],
    ["docs/site/.vitepress/config.ts", 'logo: { src: "/logo-mark.svg"', "docs/site/public/logo-mark.svg"],
  ];
  for (const [page, markup, file] of LINKS)
    test(`${page} → ${file}`, () => {
      expect(read(page), page).toContain(markup);
      expect(exists(file), file).toBe(true);
    });
});

describe("the old marks are gone from the surfaces #1590 owns", () => {
  // The top bar adopted them in #1589; the new-session landing (#1458) does in its own PR.
  const OWNED = [
    "web/src/app/App.tsx",
    "web/src/app/App.css",
    "web/public/favicon.svg",
    "web/public/icon.svg",
    "web/public/icon-maskable.svg",
    "web/public/og-image.svg",
    "web/index.html",
    "web/share.html",
    "web/connect.html",
    "src/agent_sessions/templates/login.html",
    "src/agent_sessions/templates/login_totp.html",
    "src/agent_sessions/templates/change_password.html",
    "landing/index.html",
    "landing/legal.html",
    "landing/privacy.html",
    "landing/og.svg",
    "web/src/routes/Onboarding.tsx",
    "web/src/whatsnew/WhatsNewDialog.tsx",
    "web/src/share/ShareViewer.tsx",
    "docs/site/.vitepress/config.ts",
  ];
  const OLD: [string, RegExp][] = [
    ["the ◢ glyph mark", /◢/],
    ["the rounded >_ tile", /rx="(14|112|\d+)" fill="#0d0e10"/i],
    ["the old >_ stroke", /M20 19 L33 32 L20 45|M144 152 L248 256 L144 360/],
    ["a data-URI favicon", /rel="icon"[^>]*href="data:/],
    ["the font-set wordmark card", /<tspan[^>]*>BATTLE</],
  ];
  for (const rel of OWNED)
    test(rel, () => {
      const text = read(rel);
      for (const [what, re] of OLD) expect(re.test(text), `${rel}: ${what}`).toBe(false);
    });

  test("the top bar shows the kit's mark and wordmark through the shared components (#1589)", () => {
    const app = read("web/src/app/App.tsx");
    expect(app).toContain("<BrandMark");
    expect(app).toContain("<BrandWordmark");
  });

  test("the SPA surfaces show the kit's logo through the shared components", () => {
    expect(read("web/src/routes/Onboarding.tsx")).toContain("<BrandMark");
    expect(read("web/src/share/ShareViewer.tsx")).toContain("<BrandMark");
    expect(read("web/src/whatsnew/WhatsNewDialog.tsx")).toContain("<BrandLockup");
  });

  test("Settings → About shows the lockup, not a text wordmark", () => {
    const about = read("web/src/routes/Settings.tsx");
    const at = about.indexOf('id="about-h"');
    expect(at).toBeGreaterThan(0);
    const section = about.slice(at, at + 600);
    expect(section).toContain("<BrandLockup");
    expect(section).not.toMatch(/Battle<b>Lab<\/b>/);
  });
});
