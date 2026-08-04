import { expect, test, type Locator } from "@playwright/test";

// #415: on touch, the .touchLayer overlay swallows every tap (it only (re)opens the keyboard),
// so tapping a URL in the output never reaches xterm's WebLinksAddon. A tap on a link cell must
// open the URL. We stub the websocket to print a line at the home position and record
// window.open calls.
// #664: a URL longer than the terminal width soft-wraps across rows; the tap hit-test must
// join the wrapped rows and open the FULL URL from any of them — not a truncated first-row
// fragment, and not fall through to the keyboard on a continuation row.

// Long enough to wrap onto 3+ rows at any plausible mobile width (Pixel 7 ≈ 40–50 cols).
const WRAPPED_URL = `https://example.com/attachments/${"a".repeat(120)}`;

const fakeWs = (line: string) => `
window.__opened = [];
window.open = (url) => { window.__opened.push(url); return null; };
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "blob";
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen();
      const s = "\\x1b[H\\x1b[2J" + ${JSON.stringify(line)};
      const buf = new TextEncoder().encode(s).buffer;
      if (this.onmessage) this.onmessage({ data: buf });
      if (this.onmessage) this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
    }, 30);
  }
  send() {}
  close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;

// Tap (touchstart+touchend, no move) at 15% width on the given rendered xterm row. The y comes
// from the actual row element's rect — the terminal's real row count varies with viewport, so
// deriving y from an assumed 24 rows would land taps on the wrong row.
async function tapRow(surface: Locator, rowIndex: number): Promise<void> {
  await surface.evaluate((el, row) => {
    const rowEl = document.querySelectorAll(".xterm-rows > div")[row];
    const rr = rowEl!.getBoundingClientRect();
    const sr = el.getBoundingClientRect();
    const x = Math.round(sr.x + sr.width * 0.15);
    const y = Math.round(rr.y + rr.height * 0.5);
    const touch = new Touch({
      identifier: 1,
      target: el,
      clientX: x,
      clientY: y,
    });
    el.dispatchEvent(
      new TouchEvent("touchstart", {
        cancelable: true,
        bubbles: true,
        touches: [touch],
      }),
    );
    el.dispatchEvent(
      new TouchEvent("touchend", {
        cancelable: true,
        bubbles: true,
        touches: [],
      }),
    );
  }, rowIndex);
}

async function openSession(
  page: import("@playwright/test").Page,
  line: string,
  slug: string,
) {
  await page.addInitScript(fakeWs(line));
  await page.goto(`/s/claude/${slug}`);
  const surface = page.locator("[data-touch-surface]");
  await expect(surface).toBeVisible();
  // Wait until the URL has actually rendered (avoids racing grid-stable connect).
  await expect
    .poll(async () => page.locator(".xterm-screen").innerText(), {
      timeout: 5000,
    })
    .toContain("example.com");
  return surface;
}

const openedUrls = (page: import("@playwright/test").Page) =>
  page.evaluate(() => (window as unknown as { __opened: string[] }).__opened);

test("tap on a link opens the URL", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  const surface = await openSession(
    page,
    "https://example.com/foo",
    "linktaptest",
  );
  await tapRow(surface, 0);

  await expect
    .poll(() => openedUrls(page), { timeout: 3000 })
    .toContain("https://example.com/foo");
});

test("tap on a wrapped URL's continuation row opens the full URL (#664)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  const surface = await openSession(page, WRAPPED_URL, "linktapwrap1");
  // Row 1 is a soft-wrap continuation of the URL (it spans 3+ rows at mobile widths).
  // Today this finds no https:// prefix on the row and just refocuses the keyboard.
  await tapRow(surface, 1);

  await expect
    .poll(() => openedUrls(page), { timeout: 3000 })
    .toContain(WRAPPED_URL);
});

test("tap on a wrapped URL's first row opens the full URL, not a truncated fragment (#664)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  const surface = await openSession(page, WRAPPED_URL, "linktapwrap2");
  // Today this opens only the first row's slice of the URL — a dead link.
  await tapRow(surface, 0);

  await expect
    .poll(async () => (await openedUrls(page)).length, { timeout: 3000 })
    .toBe(1);
  expect(await openedUrls(page)).toEqual([WRAPPED_URL]);
});
