import { expect, type Page, test } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

// Settings → Host was unreadable on a phone (2026-09-14 report). Each dt/dd pair is wrapped in a
// `.metaRow` div, and that div was a grid item of `.meta` — itself the two-column label/value grid
// — so two whole ROWS sat side by side ("OS …   Platform …") and the right-hand value was squeezed
// to a character per line. #289's `overflow-wrap: anywhere` let it wrap instead of overflow, which
// is why the 360px no-overflow check in settings-nav never saw it. A layout claim, so it is
// asserted on boxes in a real browser: one pair per line, labels and values in aligned columns.

/** The values from the report's screenshot — long enough to reproduce the squeeze. */
const SYSTEM = {
  os: "Linux 6.8.0-139-generic",
  platform: "Linux-6.8.0-139-generic-x86_64-with-glibc2.39",
  arch: "x86_64",
  cpus: 64,
  load: { "1": 39.73, "5": 30.1, "15": 21.4 },
  mem_total: 250 * 2 ** 30,
  mem_available: 190 * 2 ** 30,
  disk_total: 1004 * 2 ** 30,
  disk_free: 285 * 2 ** 30,
  uptime_seconds: 7 * 86_400 + 2 * 3_600,
  python: "3.12.3",
};

/** The same network settings-nav.spec mocks, with a real Host payload. */
async function mockHost(page: Page) {
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: SYSTEM }));
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
}

test("Host reads one label/value pair per line, in two aligned columns", async ({
  page,
}) => {
  await mockHost(page);
  await page.goto(settingsPath("system"));
  const dl = page.locator("section[aria-labelledby='system-h'] dl");
  await expect(dl.getByText("3.12.3", { exact: true })).toBeVisible();

  const rows = await dl.evaluate((el) =>
    Array.from(el.querySelectorAll("dt")).map((dt) => {
      const dd = dt.nextElementSibling as HTMLElement;
      const a = dt.getBoundingClientRect();
      const b = dd.getBoundingClientRect();
      return {
        label: dt.textContent ?? "",
        dtX: a.left,
        dtH: a.height,
        ddX: b.left,
        top: b.top,
        bottom: b.bottom,
        ddH: b.height,
      };
    }),
  );
  expect(rows.map((r) => r.label)).toEqual([
    "OS",
    "Platform",
    "CPU",
    "Memory",
    "Disk",
    "Uptime",
    "Python",
  ]);

  for (const r of rows) {
    expect(r.dtX, `${r.label}: label in the label column`).toBeCloseTo(rows[0].dtX, 0);
    expect(r.ddX, `${r.label}: value in the value column`).toBeCloseTo(rows[0].ddX, 0);
  }
  for (let i = 1; i < rows.length; i++) {
    expect(
      rows[i].top,
      `${rows[i].label} starts below ${rows[i - 1].label}`,
    ).toBeGreaterThanOrEqual(rows[i - 1].bottom);
  }
  // The squeeze itself: short values stay on one line. Platform is a single long token and may
  // legitimately wrap on a phone, so it is the one row exempt.
  for (const r of rows.filter((r) => r.label !== "Platform")) {
    expect(r.ddH, `${r.label} value fits on one line`).toBeLessThanOrEqual(r.dtH * 1.5);
  }
});
