// Renders banner.html (#1544): every frame is drawn at an exact time by the scene's own clock
// (`renderAt(t)`), with the portrait loop seeked to match, captured at 2x, then encoded with ffmpeg:
//   docs/banner.webp                     the README's animated WebP (1200x600)
//   landing/banner-lupu-wide.{webm,mp4,jpg}  the landing's desktop video + poster (1920x960)
//   landing/banner-lupu-tall.{webm,mp4,jpg}  the landing's phone video + poster (720x1200)
// Run from web/:  node visual/banner/render.mjs [--only wide|tall]
// Exits 1 if any output is missing or empty.
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "@playwright/test";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "../../..");
const FPS = 24;
const POSTER_AT = 18; // every line typed and the install line shown
const LAYOUTS = {
  wide: { viewport: { width: 1200, height: 600 }, video: "1920:960" },
  tall: { viewport: { width: 600, height: 1000 }, video: "720:1200" },
};
const only = process.argv.includes("--only") ? process.argv[process.argv.indexOf("--only") + 1] : null;

const ff = (args) => execFileSync("ffmpeg", ["-y", "-loglevel", "error", ...args], { stdio: "inherit" });
const outputs = [];

async function capture(browser, name, { viewport }, dir) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 2 });
  await page.goto(`${pathToFileURL(join(here, "banner.html")).href}?capture&layout=${name}`);
  await page.evaluate(() => document.fonts.ready);
  await page.waitForFunction(() => document.getElementById("v").readyState >= 2);
  const seconds = await page.evaluate(() => {
    document.getElementById("v").pause();
    return window.SCENE_SECONDS;
  });
  const frames = Math.round(seconds * FPS);
  for (let i = 0; i < frames; i++) {
    const t = i / FPS;
    await page.evaluate(async (t) => {
      window.renderAt(t);
      const v = document.getElementById("v");
      const target = t % v.duration;
      if (Math.abs(v.currentTime - target) > 0.001) {
        await new Promise((done) => {
          v.addEventListener("seeked", done, { once: true });
          v.currentTime = target;
        });
      }
    }, t);
    await page.screenshot({ path: join(dir, `${String(i).padStart(5, "0")}.png`) });
  }
  await page.close();
  console.log(`${name}: ${frames} frames`);
}

function encode(name, { video }, dir) {
  const input = ["-framerate", String(FPS), "-i", join(dir, "%05d.png")];
  const base = join(repo, "landing", `banner-lupu-${name}`);
  const scale = `scale=${video}:flags=lanczos`;
  ff([...input, "-vf", scale, "-c:v", "libvpx-vp9", "-crf", "33", "-b:v", "0", "-row-mt", "1",
    "-cpu-used", "2", "-pix_fmt", "yuv420p", "-an", `${base}.webm`]);
  ff([...input, "-vf", scale, "-c:v", "libx264", "-preset", "slow", "-crf", "22", "-pix_fmt", "yuv420p",
    "-movflags", "+faststart", "-an", `${base}.mp4`]);
  ff(["-i", join(dir, `${String(POSTER_AT * FPS).padStart(5, "0")}.png`), "-vf", scale, "-q:v", "3", `${base}.jpg`]);
  outputs.push(`${base}.webm`, `${base}.mp4`, `${base}.jpg`);
  if (name === "wide") {
    const webp = join(repo, "docs", "banner.webp");
    ff([...input, "-vf", "fps=10,scale=1200:600:flags=lanczos", "-c:v", "libwebp_anim", "-lossless", "0",
      "-quality", "72", "-compression_level", "6", "-loop", "0", webp]);
    outputs.push(webp);
  }
}

const work = mkdtempSync(join(here, ".frames-"));
try {
  const browser = await chromium.launch();
  try {
    for (const [name, layout] of Object.entries(LAYOUTS)) {
      if (only && only !== name) continue;
      const dir = join(work, name);
      execFileSync("mkdir", ["-p", dir]);
      await capture(browser, name, layout, dir);
      encode(name, layout, dir);
    }
  } finally {
    await browser.close();
  }
  const missing = outputs.filter((f) => !existsSync(f) || statSync(f).size === 0);
  if (missing.length) throw new Error(`not written: ${missing.join(", ")}`);
  for (const f of outputs) console.log(`wrote ${f.replace(`${repo}/`, "")} (${Math.round(statSync(f).size / 1024)} KB)`);
} catch (e) {
  console.error(`banner render FAILED: ${e}`);
  process.exitCode = 1;
} finally {
  rmSync(work, { recursive: true, force: true });
}
