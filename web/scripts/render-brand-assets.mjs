// Rasterize the brand SVGs in web/public to the PNGs the manifest + OG tags reference.
// Run after editing any of the source SVGs:  node scripts/render-brand-assets.mjs
// Uses the Chromium that Playwright already installs for e2e (no extra dependency).
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const pub = resolve(dirname(fileURLToPath(import.meta.url)), "..", "public");

// [source SVG, output PNG, pixel size {w,h}]
const JOBS = [
  ["icon.svg", "icon-192.png", { w: 192, h: 192 }],
  ["icon.svg", "icon-512.png", { w: 512, h: 512 }],
  ["icon-maskable.svg", "icon-maskable-512.png", { w: 512, h: 512 }],
  ["og-image.svg", "og-image.png", { w: 1200, h: 630 }],
];

const browser = await chromium.launch();
try {
  for (const [src, out, { w, h }] of JOBS) {
    const svg = readFileSync(resolve(pub, src), "utf8")
      // force the SVG to the exact target pixel box
      .replace(/width="\d+"/, `width="${w}"`)
      .replace(/height="\d+"/, `height="${h}"`);
    const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 1 });
    await page.setContent(`<!doctype html><meta charset="utf-8"><style>*{margin:0;padding:0}</style>${svg}`, {
      waitUntil: "networkidle",
    });
    await page.screenshot({ path: resolve(pub, out), clip: { x: 0, y: 0, width: w, height: h } });
    await page.close();
    console.log(`rendered ${out} (${w}x${h}) from ${src}`);
  }
} finally {
  await browser.close();
}
