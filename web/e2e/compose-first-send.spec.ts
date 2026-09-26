import { expect, test } from "@playwright/test";
import { mockRoster } from "./roster";
import { throttleIfAsked } from "./browserTiming";

// #533: the first compose Send into a FRESH session raced the agent's boot — the clear/paste/
// Enter frames landed before the TUI's input loop was live, the pasted text was swallowed, and
// the literal Ctrl-A of the line-clear was submitted as the whole first turn (production
// incident 2026-07-06, session 506b4314…). The fix holds the content delivery until the boot
// stream shows the agent's input is live.
// #607 made that "boot output has gone quiet" for engines with no bracketed-paste enable (Codex).
// #616 made it so for EVERY engine: ESC[?2004h is no longer an instant ready, because Claude Code
// emits it during pre-TUI setup, then switches to the alternate screen and clears it.
// Real browser: jsdom can't model the WS-open-vs-agent-boot interleaving these bugs live in.

// A WebSocket stub modelling a BOOTING agent: the socket OPENS immediately (the server accepted
// new=1 and launched) but emits NO output until the test calls __emitOutput — exactly the window
// the lost first message fell into. Input frames ({t:"i"}) are recorded for the assertions.
const BOOTING_WS = `
window.__sentInput = [];
window.__resizeFrames = 0;
window.__bootChunksReceived = 0;
window.__sockets = [];
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    window.__sockets.push(this);
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 20);
  }
  send(msg) {
    try {
      const m = JSON.parse(msg);
      if (m && m.t === "i") window.__sentInput.push(m.d);
      if (m && m.t === "r") window.__resizeFrames++;
    } catch {}
  }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
window.__emitOutput = (s) => {
  const ws = window.__sockets[window.__sockets.length - 1];
  if (!ws || ws.readyState !== 1 || !ws.onmessage)
    throw new Error("fake boot output requires an open socket and installed receiver");
  ws.onmessage({ data: new TextEncoder().encode(s).buffer });
  window.__bootChunksReceived++;
  ws.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
};
`;

declare global {
  interface Window {
    __sentInput: string[];
    __resizeFrames: number;
    __bootChunksReceived: number;
    __emitOutput: (s: string) => void;
  }
}

test.beforeEach(async ({ page }) => {
  await throttleIfAsked(page);
  await page.clock.install();
});

test("first compose Send into a fresh session waits for the agent's first paint to settle, not just ESC[?2004h (#533/#616)", async ({
  page,
}) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: "/home/u/proj",
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await mockRoster(page);
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route(/\/api\/sessions\/[^/]+\/draft$/, (r) =>
    r.fulfill({
      json: { id: "", text: "", attachments: [], updated_at: null },
    }),
  );
  await page.addInitScript(BOOTING_WS);

  // Drive the REAL fresh-launch flow (landing → Start), not a deep link: only a fresh launch
  // carries the router state that arms the gate — a deep-link attach must stay ungated (pinned
  // by compose-empty-send.spec.ts, which deep-links and sends immediately).
  await page.goto("/");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(/\/s\/claude\//);
  await expect(page.locator(".xterm")).toBeVisible();
  await expect
    .poll(async () => page.evaluate(() => window.__resizeFrames))
    .toBeGreaterThan(0);

  // Compose is collapsed by default on desktop; open it, type, Send.
  const sendBtn = page.getByRole("button", { name: /^send/i });
  if (!(await sendBtn.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await page.getByPlaceholder(/type here/i).fill("hello from the compose gate");
  await sendBtn.click();

  // While the agent is still booting (no output yet), NOTHING may reach the PTY — on the broken
  // code the clear (\x01\x0b) + bracketed paste land here and the message is lost.
  await expect(page.getByText(/waiting for agent/i)).toBeVisible();
  const during = await page.evaluate(() => window.__sentInput.slice());
  expect(during).toEqual([]);
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));

  // Claude's PRE-TUI setup arms bracketed paste first — at byte ~25, ~40 bytes before it clears
  // the screen. Nothing may be released on this chunk: the #616 bug delivered here, and the clear
  // below then wiped the paste. (Red before the fix: __sentInput is already non-empty at this
  // point, and the message never reaches the agent.)
  await page.evaluate(() =>
    window.__emitOutput("\x1b[?25h\x1b[?25l\x1b[?2004h"),
  );
  await page.clock.runFor(250);
  expect(await page.evaluate(() => window.__sentInput.slice())).toEqual([]);
  await expect(page.getByText(/waiting for agent/i)).toBeVisible();

  // The paint the old gate raced: alternate-screen switch, clear, then the banner.
  await page.evaluate(() => window.__emitOutput("\x1b[?1049h\x1b[2J\x1b[H"));
  await page.evaluate(() =>
    window.__emitOutput("\x1b[?1000h\x1b[?1006h✳ Claude Code\r\n❯ "),
  );
  expect(await page.evaluate(() => window.__bootChunksReceived)).toBe(3);
  await page.clock.runFor(399);
  expect(await page.evaluate(() => window.__sentInput.slice())).toEqual([]);
  await page.clock.runFor(1);

  // Paint settled → the held message delivers: clear, paste, then the deferred Enter (#180
  // sequencing preserved).
  // Readiness releases the delivery, and the CLEAR goes out ALONE: the paste follows a
  // CLEAR_DELAY_MS later so the two can never land in one pty read (#1062).
  await expect
    .poll(async () => page.evaluate(() => window.__sentInput.slice()))
    .toEqual(["\x01\x0b"]);
  await page.clock.runFor(100); // > CLEAR_DELAY_MS (80)
  await expect
    .poll(async () => page.evaluate(() => window.__sentInput.join("")))
    .toContain("\x1b[200~hello from the compose gate\x1b[201~");
  await page.clock.runFor(250); // the deferred Enter follows the paste
  await expect
    .poll(async () => page.evaluate(() => window.__sentInput.at(-1)))
    .toBe("\r");
  const frames = await page.evaluate(() => window.__sentInput.slice());
  expect(frames[0]).toBe("\x01\x0b"); // the line-clear precedes the paste, never a lone turn
  // Delivered → the composer cleared (the draft is gone with it).
  await expect(page.getByPlaceholder(/type here/i)).toHaveValue("");
});

test("fresh Codex send waits for boot output to go quiet before using fallback readiness (#607)", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "desktop",
    "timing-sensitive fallback path runs once",
  );
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["codex"],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: "/home/u/proj",
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await mockRoster(page);
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route(/\/api\/sessions\/[^/]+\/draft$/, (r) =>
    r.fulfill({
      json: { id: "", text: "", attachments: [], updated_at: null },
    }),
  );
  await page.addInitScript(BOOTING_WS);

  await page.goto("/");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(/\/s\/codex\/new-/);
  await expect(page.locator(".xterm")).toBeVisible();
  // Mounting xterm precedes connection. Emitting earlier silently dropped one or BOTH boot
  // chunks, so this test either passed vacuously or never reached readiness (#1016).
  await expect
    .poll(async () => page.evaluate(() => window.__resizeFrames))
    .toBeGreaterThan(0);

  const sendBtn = page.getByRole("button", { name: /^send/i });
  if (!(await sendBtn.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await page.getByPlaceholder(/type here/i).fill("do the first codex task");
  await sendBtn.click();
  await expect(page.getByText(/waiting for agent/i)).toBeVisible();
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));

  // Codex-style boot frames without ESC[?2004h: every chunk resets the fallback timer.
  // Broken behavior sent 1.5s after the FIRST output chunk, while startup was still painting.
  await page.evaluate(() => window.__emitOutput("loading codex…"));
  await page.clock.runFor(1000);
  await page.evaluate(() => window.__emitOutput("still preparing…"));
  expect(await page.evaluate(() => window.__bootChunksReceived)).toBe(2);
  await page.clock.runFor(700);
  expect(await page.evaluate(() => window.__sentInput.slice())).toEqual([]);
  await page.clock.runFor(800);

  // Readiness releases the delivery, and the CLEAR goes out ALONE: the paste follows a
  // CLEAR_DELAY_MS later so the two can never land in one pty read (#1062).
  await expect
    .poll(async () => page.evaluate(() => window.__sentInput.slice()))
    .toEqual(["\x01\x0b"]);
  await page.clock.runFor(100); // > CLEAR_DELAY_MS (80)
  await expect
    .poll(async () => page.evaluate(() => window.__sentInput.join("")))
    .toContain("\x1b[200~do the first codex task\x1b[201~");
  await page.clock.runFor(250);
  await expect
    .poll(async () => page.evaluate(() => window.__sentInput.at(-1)))
    .toBe("\r");
});
