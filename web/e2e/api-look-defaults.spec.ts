// The API-session look's "defaults change nothing" guarantee (#1494 P3), held in a real browser.
// Every element of a default StructuredPane (codex-api) and a default ChatPane (apichat), in dark
// and light, on desktop and mobile, must compute exactly the styles recorded from `main` BEFORE
// the look existed — e2e/fixtures/api-look-baseline.json. Re-record only on purpose:
//   API_LOOK_BASELINE_WRITE=1 npx playwright test e2e/api-look-defaults.spec.ts --workers=1
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { computedLook, mockApiPanes, openChat, openStructured } from "./apiLookFixture";

const FILE = resolve(process.cwd(), "e2e/fixtures/api-look-baseline.json");
const WRITE = process.env.API_LOOK_BASELINE_WRITE === "1";

type Look = Record<string, Record<string, string>>;
/** On disk, deduplicated: most elements share a handful of style sets, so each case keeps a table
 *  of distinct sets and maps every DOM path to one of them. */
type Stored = Record<string, { styles: Record<string, string>[]; paths: Record<string, number> }>;

function load(): Stored {
  return existsSync(FILE) ? (JSON.parse(readFileSync(FILE, "utf8")) as Stored) : {};
}
function pack(look: Look): Stored[string] {
  const styles: Record<string, string>[] = [];
  const index = new Map<string, number>();
  const paths: Record<string, number> = {};
  for (const [path, st] of Object.entries(look)) {
    const k = JSON.stringify(st);
    if (!index.has(k)) index.set(k, styles.push(st) - 1);
    paths[path] = index.get(k)!;
  }
  return { styles, paths };
}
function unpack(c: Stored[string]): Look {
  return Object.fromEntries(Object.entries(c.paths).map(([p, i]) => [p, c.styles[i]]));
}

for (const theme of ["dark", "light"] as const) {
  for (const pane of ["structured", "chat"] as const) {
    test(`a default ${pane} pane computes today's look exactly (${theme})`, async ({ page }, info) => {
      await mockApiPanes(page, { theme });
      if (pane === "structured") await openStructured(page);
      else await openChat(page);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      const look = await computedLook(page, pane === "structured" ? "structured-pane" : "chat-pane");
      const key = `${info.project.name}/${pane}/${theme}`;
      if (WRITE) {
        const all = load();
        all[key] = pack(look);
        const sorted = Object.fromEntries(Object.keys(all).sort().map((k) => [k, all[k]]));
        writeFileSync(FILE, JSON.stringify(sorted) + "\n");
        return;
      }
      const stored = load()[key];
      expect(stored, `no baseline recorded for ${key}`).toBeTruthy();
      const want = unpack(stored);
      // Same elements, same order...
      expect(Object.keys(look)).toEqual(Object.keys(want));
      // ...and every recorded property identical, reported per element so a diff names its node.
      for (const path of Object.keys(want)) expect(look[path], path).toEqual(want[path]);
    });
  }
}
