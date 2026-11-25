/** deploy-connect.yml's public-asset guard (#1705, #1776; Hermes on #1794): every root-relative
 *  asset shape is caught — flat or nested, literal or template — and HQ's page routes are not. */
import { expect, test } from "vitest";

import { rootRelativeAssets } from "../../scripts/check-connect-assets.mjs";

const caught = [
  '"/hq/europe-man.jpg"',
  '"/hq/a.mp4?v=1"',
  "`/hq/${file}`", // the #1705 shape
  '"/onboarding/files.webp"',
  "`/onboarding/${shot}`",
  '"/whatsnew/0.20/missions.svg"', // the versioned What's New layout
  "`/whatsnew/0.20/${f}`",
  "`/whatsnew/x.svg`",
];
const passes = [
  "`/hq/c/${encodeURIComponent(e)}`", // HQ's conversation route
  '"/hq/d/x"', // a dispatch route
  "`/hq/`", // the isHqPath prefix
  "`/hq/*`", // a route pattern
  '"/hq"',
  "`${B}hq/${f}`", // base-prefixed, as contacts.ts builds it
  "`${B}whatsnew/0.20/${f}`",
];

test.each(caught)("caught: %s", (s) => {
  expect(rootRelativeAssets(`x=${s};`)).not.toEqual([]);
});

test.each(passes)("passes: %s", (s) => {
  expect(rootRelativeAssets(`x=${s};`)).toEqual([]);
});
