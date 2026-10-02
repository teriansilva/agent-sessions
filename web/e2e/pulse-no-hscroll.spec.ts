import { expect, test } from "@playwright/test";

import { missionList, missionRow, mockMissions } from "./mission-console";

// #494: the Pulse view must NOT scroll horizontally on a phone. Root cause: `.pulse` set
// overflow-y:auto, which promotes overflow-x to auto, so a long unbroken token in an AI
// summary / intervention reason / banner forced the panel wider than the viewport. This proves
// (red→green) that nothing on /mission is horizontally scrollable at narrow widths.
//
// WHERE THE LONG TOKENS LIVE NOW (#948 P3): the session cards and their filter chips went with the
// "Sessions without a mission" view, so entering /mission shows the new-mission page. The text an
// operator does not control on that page is the missions' own — titles in the NEEDS YOU preview
// and the rail, and project names in the mission filter — so that is where the unbroken token is
// planted. The #803 folder-path chip this spec also exercised belonged to the removed untracked
// filter row; the sessions sidebar's own chips are pinned in `sidebar/Filters.test.tsx`.

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
};

// A genuinely long, unbroken token — the exact thing that used to force a sideways scroll.
const LONG =
  "AGENT_SESSIONS_FORCE_PASSWORD_CHANGE_supercalifragilistic_0123456789_abcdefghij";
const T = 1_700_000_000;

// A folder path as the server emits it — long, and unbreakable at a slash-free width.
const SCRATCH =
  "/tmp/claude-1000/-home-u-claude-agent-sessions/337e9b61-b91d-4d6c-8013-19865f22b34f/scratchpad/work-claude-208de4e6";

const OVERVIEW = {
  cache_version: 1,
  generated_at: T - 60,
  window_days: 3,
  scan_depth: "fast",
  input_fingerprint: "fp",
  synthesis_skipped: false,
  banner: `State of your work: the ${LONG} token in this banner must wrap, never scroll.`,
  cards: [],
};

const MISSIONS = missionList([
  missionRow({
    id: "msn_1",
    title: `Awaiting choice on ${LONG} before it can proceed`,
    project_id: "p-long",
    needs_you: true,
    updated_at: T - 120,
  }),
  missionRow({
    id: "msn_2",
    title: `Debugging ${LONG} in the build graph`,
    project_id: "p-long-2",
    needs_you: true,
    updated_at: T - 300,
  }),
]);
// The filter bar's project select lists these, so its option labels carry the long names too.
(MISSIONS.facets as { projects: string[] }).projects = ["p-long", "p-long-2"];

// Two projects sharing a name, so the console disambiguates them with their (long) folder.
const PROJECTS = {
  projects: [
    { id: "p-long", name: `proj-${LONG}`, folders: [SCRATCH] },
    { id: "p-long-2", name: `proj-${LONG}`, folders: [`${SCRATCH}-two`] },
  ],
};

for (const width of [360, 390]) {
  test(`Pulse never scrolls horizontally at ${width}px (#494)`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 780 });
    await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
    await page.route("**/api/sessions**", (r) =>
      r.fulfill({
        json: {
          sessions: [],
          next_offset: null,
          total: 0,
          facets: { projects: [], engines: [] },
        },
      }),
    );
    await page.route("**/api/version", (r) =>
      r.fulfill({ json: { version: "test" } }),
    );
    await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
    await page.route("**/api/pulse", (r) => r.fulfill({ json: OVERVIEW }));
    await page.route(/\/api\/projects($|\?)/, (r) =>
      r.fulfill({ json: PROJECTS }),
    );

    await mockMissions(page, { missions: MISSIONS });
    await page.goto("/mission");
    await expect(
      page.getByTestId("landing-needs-row").filter({ hasText: /Awaiting choice/i }),
    ).toBeVisible();

    const result = await page.evaluate(() => {
      // User-visible horizontal scroll = an element whose overflow-x is auto/scroll AND whose
      // content is wider than its box. (overflow:hidden / ellipsis elements are programmatically
      // scrollable but the USER can't pan them, so they don't count.)
      const offenders: string[] = [];
      for (const el of Array.from(
        document.querySelectorAll<HTMLElement>("body *"),
      )) {
        const ox = getComputedStyle(el).overflowX;
        if (
          (ox === "auto" || ox === "scroll") &&
          el.scrollWidth > el.clientWidth + 1
        ) {
          offenders.push(
            `${el.tagName}.${String(el.className).slice(0, 40)} sw=${el.scrollWidth} cw=${el.clientWidth}`,
          );
        }
      }
      // The Pulse scroll container must also genuinely FIT its content (not merely clip it) — this
      // is what goes red on the unfixed code, where a long token forces scrollWidth > clientWidth.
      const pulse = document.querySelector<HTMLElement>('[class*="_pulse_"]');
      const de = document.documentElement;
      return {
        offenders,
        pulseFit: pulse ? pulse.scrollWidth - pulse.clientWidth : -1,
        pulseFound: !!pulse,
        docOverflow: de.scrollWidth - de.clientWidth,
      };
    });

    expect(result.pulseFound).toBe(true);
    expect(result.offenders).toEqual([]);
    expect(result.pulseFit).toBeLessThanOrEqual(1);
    expect(result.docOverflow).toBeLessThanOrEqual(0);
  });
}
