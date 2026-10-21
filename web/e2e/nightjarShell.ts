/** A mocked shell for the NIGHTJAR specs (#1531): enough of the API for the dashboard, the
 *  primitives gallery and Settings to render with no backend. Every unknown `/api/**` answers `{}`. */
import type { Page } from "@playwright/test";

export async function mockNightjarShell(page: Page, opts: { hqContact?: string } = {}) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
        hq_contact: opts.hqContact ?? "romania-woman",
        hq_contact_rev: 1,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null } }),
  );
}

/** Store a motion choice for the page's origin BEFORE the app boots (the pre-paint script reads it). */
export async function storeMotionChoice(page: Page, choice: "full" | "reduced" | "off" | null) {
  await page.addInitScript((c) => {
    try {
      if (c) localStorage.setItem("tr-motion", c);
      else localStorage.removeItem("tr-motion");
    } catch {
      /* storage blocked */
    }
  }, choice);
}
