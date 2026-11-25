#!/usr/bin/env node
/** The connect build's public-asset guard (#1705, #1776), run by deploy-connect.yml.
 *
 *  Under the relay the app lives at `/connect/`, and a ROOT-relative public asset (`/hq/…`,
 *  `/onboarding/…`, `/whatsnew/…`) is the landing site's 301 there, not the file. Every asset is
 *  built under the build's base (`${BASE}hq/…`, `publicAsset(…)`), so a root-relative one in the
 *  bundle is a bug.
 *
 *  What counts as an ASSET (and nothing else, so HQ's page ROUTES — `/hq/c/<id>`, the `/hq/`
 *  prefix, `/hq/*` — which go through the router's basename, never trip it):
 *  - a FILE name at any depth under those folders: `"/hq/europe-man.jpg"`,
 *    `"/whatsnew/0.20/missions.svg"` (Hermes on #1794);
 *  - a TEMPLATE interpolating into those folders: anywhere under `onboarding` or `whatsnew`
 *    (`` `/whatsnew/0.20/${f}` ``), and straight after `/hq/` (`` `/hq/${file}` ``, the #1705 shape —
 *    portraits sit flat; deeper `/hq/c/${…}` is a route).
 *  Not caught: a bare `"/hq/"` concatenated with a file name — that string is a route prefix now.
 *
 *  `node scripts/check-connect-assets.mjs <dir>` scans every `*.js` in `<dir>`, prints each hit and
 *  exits 1 on any. */
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const EXT = "(?:jpe?g|png|webp|gif|svg|mp4|webm|json|txt|ico)";
const FILE = new RegExp(`["\`]/(?:onboarding|hq|whatsnew)/[^"\`\\s]*\\.${EXT}(?=["\`?#])`, "g");
const TEMPLATE = /`\/(?:(?:onboarding|whatsnew)\/[^`\s]*|hq\/)\$\{/g;

/** Every root-relative public-asset URL in `text`. */
export function rootRelativeAssets(text) {
  return [...text.matchAll(FILE), ...text.matchAll(TEMPLATE)].map((m) => m[0]);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const dir = process.argv[2];
  if (!dir) {
    console.error("usage: check-connect-assets.mjs <assets dir>");
    process.exit(2);
  }
  let found = 0;
  for (const name of readdirSync(dir).filter((n) => n.endsWith(".js"))) {
    for (const hit of rootRelativeAssets(readFileSync(join(dir, name), "utf8"))) {
      console.error(`${name}: ${hit}`);
      found += 1;
    }
  }
  if (found) {
    console.error("connect build contains root-relative public asset URLs");
    process.exit(1);
  }
}
