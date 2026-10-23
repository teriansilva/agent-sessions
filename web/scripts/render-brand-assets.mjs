// Write every derived copy of the BattleLab logo from web/src/brand (#1590) — see brand.mjs —
// then rasterize the PNGs the platforms require (manifest, maskable, apple-touch, OG).
// Run after changing anything in web/src/brand:  node scripts/render-brand-assets.mjs
// brandAssets.test.ts fails while the repo and this output disagree.
// Uses the Chromium that Playwright already installs for e2e (no extra dependency).
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { chromium } from "@playwright/test";

import { RASTERS, REPO, SLOTTED, brandFiles, fillSlots } from "./brand.mjs";

for (const [rel, text] of Object.entries(brandFiles())) {
  const out = resolve(REPO, rel);
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, text);
  console.log(`wrote ${rel}`);
}
for (const rel of SLOTTED) {
  const path = resolve(REPO, rel);
  const before = readFileSync(path, "utf8");
  const after = fillSlots(before);
  if (after !== before) writeFileSync(path, after);
  console.log(`${after === before ? "current" : "filled"} ${rel}`);
}

const browser = await chromium.launch();
try {
  for (const [srcRel, outRel, w, h] of RASTERS) {
    const svg = readFileSync(resolve(REPO, srcRel), "utf8")
      .replace(/width="\d+"/, `width="${w}"`)
      .replace(/height="\d+"/, `height="${h}"`);
    const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 1 });
    await page.setContent(
      `<!doctype html><meta charset="utf-8"><style>*{margin:0;padding:0}html,body{background:transparent}</style>${svg}`,
      { waitUntil: "networkidle" },
    );
    await page.screenshot({ path: resolve(REPO, outRel), clip: { x: 0, y: 0, width: w, height: h }, omitBackground: true });
    await page.close();
    console.log(`rendered ${outRel} (${w}x${h}) from ${srcRel}`);
  }
} finally {
  await browser.close();
}
