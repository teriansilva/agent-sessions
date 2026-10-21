/** The engine roster for E2E mocks (#853 P4): the SAME fixture the unit tests render with —
 *  generated from the plugin manifests and pinned against `/api/engines` by
 *  `tests/test_web_roster_fixture.py` — so a mocked app sees exactly what the real server serves.
 *  A hand-written `engines` list (or `[]`) leaves the SPA without the roster, and everything that
 *  must not be guessed (id mode, handoff eligibility, id prefix) correctly waits. */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";

type Row = Record<string, unknown> & { id: string };

export const ROSTER: { engines: Row[]; problems: unknown[] } = JSON.parse(
  readFileSync(fileURLToPath(new URL("../src/test/roster.fixture.json", import.meta.url)), "utf8"),
);

/** Serve the roster, optionally narrowed and with per-engine host overrides
 *  (`{ gemini: { present: false } }`). Registered LAST wins, so call it after a catch-all. */
export async function mockRoster(
  page: Page,
  opts: { only?: string[]; overrides?: Record<string, Partial<Row>> } = {},
): Promise<void> {
  const engines = ROSTER.engines
    .filter((e) => !opts.only || opts.only.includes(e.id))
    .map((e) => ({ ...e, ...(opts.overrides?.[e.id] ?? {}) }));
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines, problems: [] } }),
  );
}

/** The runtime map a RETURNING browser holds (#1502): what `engineRoster.ts` writes to
 *  `localStorage` from every loaded roster, derived here from the same fixture. The suite's
 *  default storage state carries it (playwright.config.ts), so a spec opens a session page the way
 *  an operator's browser does after its first visit — the right pane on the first render. A spec
 *  about the FIRST visit starts without it (`e2e/api-deeplink-runtime.spec.ts`). */
export const RUNTIME_HINTS_KEY = "battlelab.engineRuntimes.v1";
export function runtimeHintsValue(
  runtimes: Record<string, string> = Object.fromEntries(
    ROSTER.engines.map((e) => [e.id, String(e.runtime)]),
  ),
): string {
  return JSON.stringify({ v: 1, runtimes });
}
