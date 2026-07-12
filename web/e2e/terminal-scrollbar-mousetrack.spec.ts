import { expect, test } from "@playwright/test";

// Real claude arms mouse tracking (confirmed via the app's `.modes` sidecar: 1000,1002,1003,1006,
// 2004), so xterm forwards the wheel to the agent and its own viewport never leaves the tail — the
// native scrollbar sits stuck at the bottom and just misleads. Hide it on agent-owns-scroll
// sessions (mouse-tracking or alt-screen); keep it on plain scrollback sessions (codex / gemini /
// antigravity, which run inline on the normal buffer with no mouse). Real browser required: the
// scrollbar chrome + xterm's mouse routing aren't modelled by jsdom.

const stub = (prefix: string, body: string) => `
window.WebSocket = class {
  constructor(url){ this.url=url; this.readyState=0; this.binaryType="arraybuffer";
    setTimeout(()=>{ this.readyState=1; this.onopen&&this.onopen();
      const s = ${JSON.stringify(prefix)} + ${JSON.stringify(body)};
      const buf = new TextEncoder().encode(s).buffer;
      this.onmessage&&this.onmessage({data:buf});
      this.onmessage&&this.onmessage({data:JSON.stringify({t:"seq",n:s.length})});
    },30);
  }
  send(){}
  close(){ this.readyState=3; this.onclose&&this.onclose({code:1000}); }
};`;

// claude's real attach: mouse tracking + SGR + bracketed paste (matches the .modes sidecar).
const CLAUDE = "\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?2004h\x1b[H\x1b[2J";
// codex / gemini / antigravity: inline on the normal buffer, bracketed paste only, NO mouse.
const PLAIN = "\x1b[?2004h\x1b[H\x1b[2J";
const body = Array.from({ length: 120 }, (_, i) => `line ${i} ---------------`).join("\r\n");

async function open(page: import("@playwright/test").Page, prefix: string, id: string) {
  await page.addInitScript(stub(prefix, body));
  await page.goto(`/s/claude/${id}`);
  await expect
    .poll(async () => page.locator(".xterm-screen").innerText(), { timeout: 6000 })
    .toContain("line 1");
}
const ownsAttr = (page: import("@playwright/test").Page) =>
  page.evaluate(() => document.querySelector("[data-app-owns-scroll]")?.getAttribute("data-app-owns-scroll") ?? null);
const scrollbarHidden = (page: import("@playwright/test").Page) =>
  page.evaluate(() => {
    const v = document.querySelector(".xterm-viewport");
    return v ? getComputedStyle(v).scrollbarWidth === "none" : null;
  });

test("mouse-tracking session (claude) hides the useless native scrollbar", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "scrollbar chrome is a desktop concern");
  await open(page, CLAUDE, "sb-claude");
  // GREEN with the fix: mouse mode armed in the stream → the agent owns the scroll → bar hidden.
  await expect.poll(() => ownsAttr(page), { timeout: 4000 }).toBe("true");
  expect(await scrollbarHidden(page)).toBe(true);
});

test("plain scrollback session (codex/antigravity) keeps its working scrollbar", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "scrollbar chrome is a desktop concern");
  await open(page, PLAIN, "sb-plain");
  // No mouse tracking → xterm keeps real scrollback → the bar is functional, must stay visible.
  await expect.poll(() => ownsAttr(page), { timeout: 4000 }).toBe("false");
  expect(await scrollbarHidden(page)).toBe(false);
});
