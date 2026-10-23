// The BattleLab logo, from ONE source (#1590). `web/src/brand/` holds the brand kit's SVGs verbatim;
// every other place the logo ships is derived here: the favicon and PWA icon sources, the OG cards,
// the landing and docs copies, and the inline slots in pages that must not fetch anything (the
// server-rendered sign-in pages and the standalone connect shell). Pure Node: the generator
// (render-brand-assets.mjs) writes these, and brandAssets.test.ts proves the repo matches them.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const WEB = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const REPO = resolve(WEB, "..");
export const BRAND = resolve(WEB, "src", "brand");

const src = (name) => readFileSync(resolve(BRAND, name), "utf8").trim();

/** The kit's dark ground (its avatar and icon backdrops). */
export const GROUND = "#07080A";

/** The inner markup of an SVG (everything between its root tags) and its viewBox. */
function parts(svg) {
  const viewBox = /viewBox="([^"]+)"/.exec(svg)[1];
  const inner = svg.replace(/^<svg[^>]*>/, "").replace(/<\/svg>\s*$/, "");
  return { viewBox, inner };
}

/** An SVG for inlining in HTML: no fixed size (CSS sizes it), a class, and an accessible role. */
function inline(name, { cls, label } = {}) {
  const { viewBox, inner } = parts(src(name));
  const a11y = label ? `role="img" aria-label="${label}"` : `aria-hidden="true" focusable="false"`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox}"${cls ? ` class="${cls}"` : ""} ${a11y}>${inner}</svg>`;
}

/** The mark on the dark ground, centred at `ratio` of the tile: inside a maskable icon's safe
 *  zone (a circle of 80% of the tile) at 0.55, so no platform mask clips the plate. */
function markOnGround(size, ratio) {
  const { inner } = parts(src("mark-amber.svg"));
  const scale = (size * ratio) / 64;
  const off = (size - 64 * scale) / 2;
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">` +
    `<rect width="${size}" height="${size}" fill="${GROUND}"/>` +
    `<g transform="translate(${off} ${off}) scale(${scale})">${inner}</g></svg>`
  );
}

/** The 1200 × 630 social card: the tagline lockup on the dark ground inside a chamfered frame. */
function ogCard() {
  const { inner } = parts(src("logo-horizontal-on-dark.svg"));
  const w = 1200;
  const h = 630;
  const scale = 2.4; // 368 × 64 → 883 × 154
  const x = (w - 368 * scale) / 2;
  const y = (h - 64 * scale) / 2;
  const c = 28; // the frame's chamfer, as NIGHTJAR panels cut it
  const f = 40;
  const frame = `M${f + c} ${f} H${w - f} V${h - f - c} L${w - f - c} ${h - f} H${f} V${f + c} Z`;
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">` +
    `<rect width="${w}" height="${h}" fill="${GROUND}"/>` +
    `<path d="${frame}" fill="none" stroke="#FFB000" stroke-opacity="0.45" stroke-width="2"/>` +
    `<g transform="translate(${x} ${y}) scale(${scale})">${inner}</g></svg>`
  );
}

/** Whole files derived from the kit, repo-relative. */
export function brandFiles() {
  const mark = src("mark-amber.svg") + "\n";
  return {
    "web/public/favicon.svg": mark,
    "web/public/icon.svg": mark,
    "web/public/icon-maskable.svg": markOnGround(512, 0.55) + "\n",
    "web/public/og-image.svg": ogCard() + "\n",
    "landing/favicon.svg": mark,
    "landing/og.svg": ogCard() + "\n",
    "docs/site/public/favicon.svg": mark,
    "docs/site/public/logo-mark.svg": mark,
  };
}

/** The PNGs the platforms require, rendered from the files above: [source, output, size]. */
export const RASTERS = [
  ["web/public/icon.svg", "web/public/icon-192.png", 192, 192],
  ["web/public/icon.svg", "web/public/icon-512.png", 512, 512],
  ["web/public/icon-maskable.svg", "web/public/icon-maskable-512.png", 512, 512],
  ["web/public/icon-maskable.svg", "web/public/apple-touch-icon.png", 180, 180],
  ["web/public/og-image.svg", "web/public/og-image.png", 1200, 630],
  ["landing/og.svg", "landing/og.png", 1200, 630],
];

/** Inline slots: `<!-- brand:NAME -->…<!-- /brand:NAME -->` in pages that must not fetch the logo
 *  (pre-auth and pre-SPA pages). */
export function brandSlots() {
  return {
    "lockup-notag-dark": inline("logo-horizontal-notag-on-dark.svg", { cls: "brand-lockup", label: "BattleLab" }),
    "mark": inline("mark-amber.svg", { cls: "brand-mark" }),
    "glyph": inline("glyph-amber.svg", { cls: "brand-glyph" }),
  };
}

/** Repo-relative files that carry slots. */
export const SLOTTED = [
  "src/agent_sessions/templates/login.html",
  "src/agent_sessions/templates/login_totp.html",
  "src/agent_sessions/templates/change_password.html",
  "web/connect.html",
  "landing/index.html",
];

const SLOT = /<!-- brand:([a-z-]+) -->[\s\S]*?<!-- \/brand:\1 -->/g;

/** `text` with every slot filled from the source; throws on a slot name the source lacks. */
export function fillSlots(text) {
  const slots = brandSlots();
  return text.replace(SLOT, (_, name) => {
    if (!(name in slots)) throw new Error(`unknown brand slot: ${name}`);
    return `<!-- brand:${name} -->${slots[name]}<!-- /brand:${name} -->`;
  });
}

/** The slot names a file uses. */
export function slotNames(text) {
  return [...text.matchAll(SLOT)].map((m) => m[1]);
}
