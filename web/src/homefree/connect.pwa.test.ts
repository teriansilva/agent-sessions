// @vitest-environment node
//
// Regression (#558 review): the PWA navigation fallback serves index.html for
// every navigation except the denylist, so the standalone /connect.html page
// must be denylisted or a service-worker-controlled browser gets the SPA shell
// instead of the terminal page.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const viteConfig = readFileSync(
  fileURLToPath(new URL("../../vite.config.ts", import.meta.url)),
  "utf8",
);

describe("PWA navigation fallback", () => {
  it("denylists /connect.html so the SW never shadows it with index.html", () => {
    // The denylist entry for the standalone connect page must be present.
    expect(viteConfig).toMatch(/\/\^\\\/connect\\\.html\$\//);
  });

  it("still builds connect.html as its own entry", () => {
    expect(viteConfig).toMatch(/connect:\s*"connect\.html"/);
  });
});
