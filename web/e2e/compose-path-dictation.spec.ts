import { type Page, expect, test } from "@playwright/test";

/** Two file paths tapped into a LIVE dictation, aimed at adjacent selections (#809).
 *
 *  #794 shipped the queued-selection rebasing and got the boundary wrong: a later selection whose
 *  END equals an earlier splice's START was treated as *inside* the replaced range, so rebasing
 *  stretched it over the path that had just been inserted and the second splice deleted it.
 *
 *  A component test reproduces the arithmetic, and one exists. This drives the whole chain the
 *  component test stubs past — FilePanel row → SessionView → Terminal → Compose, with a real
 *  recognizer lifecycle in between and real focus moving from the textarea to a panel button —
 *  because that chain is what #794 changed and what regressed.
 *
 *  Dictation is push-to-talk, so a physical hold would occupy the only pointer we have; the mic's
 *  `pointerdown` is dispatched instead and never released. Everything after that is ordinary
 *  clicking in a real browser.
 */

const NOW = Math.floor(Date.now() / 1000);
const CWD = "/home/u/proj";
const SID = "aaaaaaaa-0000-4000-8000-000000000809";

const SESSION = {
  id: `claude:${SID}`,
  engine: "claude",
  title: "adjacent selections",
  cwd: CWD,
  project: { kind: "folder", id: CWD, label: "proj" },
  last_mtime: NOW - 120,
  archived: false,
  favorite: false,
};

const ENTRIES = [
  { name: "a.py", path: `${CWD}/a.py`, kind: "file", size: 10, mtime: NOW },
  { name: "b.py", path: `${CWD}/b.py`, kind: "file", size: 10, mtime: NOW },
];

/** A recognizer that speaks once and then STAYS OPEN — `stop()` does not end the session, which
 *  is what keeps the taps queued behind a handoff. The test ends it explicitly, standing in for
 *  the engine finally delivering `onend`. */
const SPEECH_STUB = `
window.__recog = { started: 0 };
window.SpeechRecognition = class {
  constructor() {
    this.continuous = false; this.interimResults = false; this.lang = "";
    this.onresult = null; this.onerror = null; this.onend = null;
    window.__recog.instance = this;
  }
  start() {
    // Only the FIRST session speaks. The re-armed one stays open and silent, the way a live
    // engine waits for more speech — a stub that re-speaks would append the phrase again and
    // hide the splice under test.
    if (++window.__recog.started > 1) return;
    setTimeout(() => { if (this.onresult) this.onresult({ resultIndex: 0,
      results: [{ 0: { transcript: "abc def ghi" }, isFinal: true, length: 1 }] }); }, 30);
  }
  stop() {}                       // deliberately silent — the tail is still "in flight"
  abort() { if (this.onend) this.onend(); }
};
window.__endRecog = () => { const r = window.__recog.instance; if (r && r.onend) r.onend(); };
`;

const GUM_STUB = `
Object.defineProperty(navigator, "mediaDevices", {
  configurable: true,
  value: { getUserMedia: () => Promise.resolve({ getTracks: () => [{ stop() {} }] }) },
});
`;

const NOOP_WS = `
window.WebSocket = class {
  constructor() { this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen(); }, 20); }
  send() {} close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;

async function mockApp(page: Page) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        hostname: "test",
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [SESSION],
        next_offset: null,
        total: 1,
        facets: { projects: [], engines: ["claude"] },
      },
    }),
  );
  await page.route("**/api/files/capabilities", (r) =>
    r.fulfill({ json: { ok: true, reason: "" } }),
  );
  await page.route("**/api/files/list**", (r) =>
    r.fulfill({
      json: {
        path: CWD,
        parent: "/home/u",
        root: "/home/u",
        entries: ENTRIES,
        total: ENTRIES.length,
        complete: true,
        truncated: false,
      },
    }),
  );
  await page.route("**/api/git/status**", (r) =>
    r.fulfill({ json: { repo: null } }),
  );
  await page.route("**/api/sessions/*/draft", (r) => r.fulfill({ json: {} }));
}

const draft = (page: Page) => page.getByPlaceholder(/Type here/i);

/** Show the file panel without navigating — a `goto` would throw away the draft under test. */
async function showPanel(page: Page) {
  if (await page.locator("[data-file-panel]").isVisible()) return;
  const direct = page.locator("[data-head-action='files']");
  if (await direct.count()) await direct.click();
  else {
    await page.getByRole("button", { name: "More session actions" }).click();
    await page.getByRole("menuitem", { name: /Files/ }).click();
  }
  await expect(page.locator("[data-file-panel]")).toBeVisible();
}

/** Aim the next tap at `[start, end)` of the draft, the way a user dragging a selection does. */
async function select(page: Page, start: number, end: number) {
  await draft(page).evaluate(
    (el, [s, e]) => (el as HTMLTextAreaElement).setSelectionRange(s, e),
    [start, end],
  );
}

test("two paths aimed at adjacent selections both survive the dictation handoff (#809)", async ({
  page,
}) => {
  await page.addInitScript(NOOP_WS);
  await page.addInitScript(SPEECH_STUB);
  await page.addInitScript(GUM_STUB);
  await mockApp(page);
  await page.goto(`/s/claude/${SID}`);
  await page.locator("[data-head-action]").first().waitFor();

  // Compose is collapsed on desktop; the mic only exists once it is open.
  const mic = page.getByRole("button", { name: /start voice input/i });
  if (!(await mic.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(mic).toBeVisible();

  // Start dictation and leave it running (see the header note on the dispatched pointerdown).
  await mic.dispatchEvent("pointerdown", {
    pointerId: 1,
    pointerType: "mouse",
    isPrimary: true,
    button: 0,
  });
  await expect(draft(page)).toHaveValue("abc def ghi");

  // Open the file panel without navigating away — that would discard the draft under test.
  await showPanel(page);

  // Tap 1 replaces "def"; tap 2 replaces "abc " — disjoint, and ending exactly where tap 1 began.
  await select(page, 4, 7);
  await page.getByRole("button", { name: "Add a.py to the message" }).click();
  // On mobile the sheet closes after a send so the draft is visible; re-open it for the second
  // tap. On desktop the dock stays put and this is a no-op.
  await showPanel(page);
  await select(page, 0, 4);
  await page.getByRole("button", { name: "Add b.py to the message" }).click();

  // The draft has not moved yet: both taps are queued behind the live recognizer.
  await expect(draft(page)).toHaveValue("abc def ghi");

  // The engine finally hangs up → the re-arm drains the queue onto the reconciled draft.
  await page.evaluate(() => (window as unknown as { __endRecog: () => void }).__endRecog());

  // Before the fix: "b.py ghi" — a.py was rebased away and then deleted by the second splice.
  await expect(draft(page)).toHaveValue("b.py a.py ghi");
});
