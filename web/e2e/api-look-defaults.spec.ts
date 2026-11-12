// The API-session look's "defaults change nothing" guarantee (#1494 P3), held in a real browser.
// Every element of a default StructuredPane (codex-api) and a default ChatPane (apichat), in dark
// and light, on desktop and mobile, must compute exactly the styles recorded from `main` BEFORE
// the look existed — e2e/fixtures/api-look-baseline.json. Re-recorded once on purpose for #1531:
// NIGHTJAR prepends the bundled "BattleLab Mono" face to --font-mono, which changed 148 font-family
// values and nothing else (verified by diffing the two files). Re-recorded a second time on purpose
// for #1568: the structured pane wears the terminal's head classes (panelHead, headActions,
// restartBtn → headChip) and the shared composer's Talk / Send / toggle, so NIGHTJAR's session
// chrome changed exactly those elements — the head bar, its label and chips, and the three composer
// controls — and nothing in the transcript, its layout, or the chat pane. A third time for #1586:
// Send's label gained its own <span> (so a 360 px composer can show the icon alone) — one added
// element per structured case, no computed style changed. A fourth time for the #1570 follow-up (the
// uplink veil on API sessions): the log moved into a conversation-area wrapper (`.logArea`, which the
// veil overlays) — one inserted element per case at the log's old position, and every one of the
// existing elements (24 chat, 87–124 structured) computes exactly the recorded styles, checked by
// mapping each old path under the wrapper. A fifth time for #1726: the desktop structured head
// gained ONE action, Plan & tools, after Files — eight inserted elements per desktop structured case
// (the button, its icon's svg and five paths, its label span); every existing element computes
// exactly the recorded styles at its shifted path, the mobile cases (one Actions menu) and the chat
// pane are unchanged, and the strip and the rail render nothing in a default pane (no background
// job, rail closed). A sixth time for #1931: every message header gained its author's avatar disc —
// two inserted elements per structured case (the default pane's one exchange: you + the agent),
// every existing element computes exactly the recorded styles in order (the header keeps its
// baseline alignment; only the disc centres itself), and the chat pane is unchanged. A seventh
// time for the removal of the one-tap Share link (#1232; Share publicly… replaces it): the desktop
// structured head lost that ONE action — eight removed elements per desktop structured case (the
// button, its icon's svg and five shapes, its label span); every remaining element computes exactly
// the recorded styles in order, and the mobile cases (one Actions menu, closed) and the chat pane
// are byte-identical.
// An eighth time for #1956: a finished reply's header gained the Share this reply icon — five
// inserted elements per structured case (the button, its svg and three paths); every existing
// element computes exactly the recorded styles in order, and the chat pane is unchanged.
// Re-record only on purpose:
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
