import { expect, test } from "@playwright/test";

// #494: the Pulse view must NOT scroll horizontally on a phone. Root cause: `.pulse` set
// overflow-y:auto, which promotes overflow-x to auto, so a long unbroken token in an AI
// summary / intervention reason / banner forced the panel wider than the viewport. This proves
// (red→green) that nothing on /pulse is horizontally scrollable at narrow widths.

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
};

// A genuinely long, unbroken token — the exact thing that used to force a sideways scroll.
const LONG = "AGENT_SESSIONS_FORCE_PASSWORD_CHANGE_supercalifragilistic_0123456789_abcdefghij";
const T = 1_700_000_000;

const OVERVIEW = {
  cache_version: 1,
  generated_at: T - 60,
  window_days: 3,
  scan_depth: "medium",
  input_fingerprint: "fp",
  synthesis_skipped: false,
  banner: `State of your work: the ${LONG} token in this banner must wrap, never scroll.`,
  cards: [
    {
      id: "claude:abc",
      engine: "claude",
      title: "Awaiting choice on the social-source invite-gating gap",
      cwd: "/home/u/webapp",
      project: { kind: "folder", id: "/home/u/webapp", name: "webapp" },
      last_activity: T - 120,
      ai_summary: null,
      intervention_required: true,
      intervention_reason: `needs a decision about ${LONG} before it can proceed`,
      reviewed_at: T - 120,
      live: false,
      state: "needs_you",
      synthesis: `presents a multi-select prompt about ${LONG} (open issue / note / explain / custom).`,
    },
    {
      id: "claude:def",
      engine: "claude",
      title: "Debugging AppHost build hang",
      cwd: "/home/u/superstatus",
      project: { kind: "folder", id: "/home/u/superstatus", name: "superstatus" },
      last_activity: T - 300,
      ai_summary: `exploring ${LONG} in the build graph`,
      intervention_required: false,
      intervention_reason: "",
      reviewed_at: T - 300,
      live: true,
      state: "in_flight",
      synthesis: null,
    },
  ],
};

for (const width of [360, 390]) {
  test(`Pulse never scrolls horizontally at ${width}px (#494)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 780 });
    await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
    await page.route("**/api/sessions**", (r) =>
      r.fulfill({
        json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
      }),
    );
    await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
    await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
    await page.route("**/api/pulse", (r) => r.fulfill({ json: OVERVIEW }));

    await page.goto("/pulse");
    await expect(page.getByText(/Awaiting choice/i)).toBeVisible();

    const result = await page.evaluate(() => {
      // User-visible horizontal scroll = an element whose overflow-x is auto/scroll AND whose
      // content is wider than its box. (overflow:hidden / ellipsis elements are programmatically
      // scrollable but the USER can't pan them, so they don't count.)
      const offenders: string[] = [];
      for (const el of Array.from(document.querySelectorAll<HTMLElement>("body *"))) {
        const ox = getComputedStyle(el).overflowX;
        if ((ox === "auto" || ox === "scroll") && el.scrollWidth > el.clientWidth + 1) {
          offenders.push(`${el.tagName}.${String(el.className).slice(0, 40)} sw=${el.scrollWidth} cw=${el.clientWidth}`);
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
