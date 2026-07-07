import { expect, test } from "@playwright/test";

// #533: the first compose Send into a FRESH session raced the agent's boot — the clear/paste/
// Enter frames landed before the TUI's input loop was live, the pasted text was swallowed, and
// the literal Ctrl-A of the line-clear was submitted as the whole first turn (production
// incident 2026-07-06, session 506b4314…). The fix holds the content delivery until the boot
// stream shows the agent arming its input (the bracketed-paste enable, ESC[?2004h).
// Real browser: jsdom can't model the WS-open-vs-agent-boot interleaving this bug lives in.

// A WebSocket stub modelling a BOOTING agent: the socket OPENS immediately (the server accepted
// new=1 and launched) but emits NO output until the test calls __emitOutput — exactly the window
// the lost first message fell into. Input frames ({t:"i"}) are recorded for the assertions.
const BOOTING_WS = `
window.__sentInput = [];
window.__sockets = [];
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    window.__sockets.push(this);
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 20);
  }
  send(msg) {
    try { const m = JSON.parse(msg); if (m && m.t === "i") window.__sentInput.push(m.d); } catch {}
  }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
window.__emitOutput = (s) => {
  const ws = window.__sockets[window.__sockets.length - 1];
  if (!ws || !ws.onmessage) return;
  ws.onmessage({ data: new TextEncoder().encode(s).buffer });
  ws.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
};
`;

declare global {
  interface Window {
    __sentInput: string[];
    __emitOutput: (s: string) => void;
  }
}

test("first compose Send into a fresh session waits for the agent's input to come live (#533)", async ({
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
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route(/\/api\/sessions\/[^/]+\/draft$/, (r) =>
    r.fulfill({ json: { id: "", text: "", attachments: [], updated_at: null } }),
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
    .poll(async () => page.evaluate(() => window.__sockets.length as number))
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

  // The agent's first paint arms bracketed paste → the held message delivers: clear, paste,
  // then the deferred Enter (#180 sequencing preserved).
  await page.evaluate(() => window.__emitOutput("\x1b[?2004h\x1b[2J\x1b[Hwelcome ❯ "));
  await expect
    .poll(async () => page.evaluate(() => window.__sentInput.join("")))
    .toContain("\x1b[200~hello from the compose gate\x1b[201~");
  await expect
    .poll(async () => page.evaluate(() => window.__sentInput.at(-1)))
    .toBe("\r");
  const frames = await page.evaluate(() => window.__sentInput.slice());
  expect(frames[0]).toBe("\x01\x0b"); // the line-clear precedes the paste, never a lone turn
  // Delivered → the composer cleared (the draft is gone with it).
  await expect(page.getByPlaceholder(/type here/i)).toHaveValue("");
});
