import { expect, test, type Page } from "@playwright/test";

// #2024: Claude Code (≥ 2.1.318) prints its /login URL as HARD-wrapped rows. Each row is its own
// OSC 8 hyperlink whose target is the COMPLETE URL, and the rows are joined by cursor moves
// (`\r ESC[1B`), not soft wraps:
//
//   ESC]8;id=X;<FULL URL>BEL <visible fragment> ESC]8;;BEL \r ESC[1B …
//
// #664's plain-text hit test only sees the visible fragment: row 1 opened a cut-off URL and every
// continuation row opened nothing, so the operator could not sign in from a phone. A tap (mobile)
// or click (desktop) on ANY row of the link must offer the link's full target in a confirm, and an
// accepted target opens through openTerminalLink. A cancelled or refused target opens nothing —
// never the visible fragment.
//
// Synthetic values only: this is the shape of the real URL, not a real one.

const TARGET =
  "https://claude.example/oauth/authorize?code=true&client_id=00000000-test-0000-0000-000000000000" +
  "&response_type=code&redirect_uri=https%3A%2F%2Fclaude.example%2Foauth%2Fcode%2Fcallback" +
  "&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference+user%3Asessions%3Aclaude_code" +
  "&code_challenge=SYNTHETICchallenge000000000000000000000000&code_challenge_method=S256" +
  "&state=SYNTHETICstate0000000000000000000000000000";

// Narrow enough to fit a phone's column count with the indent, so no row soft-wraps.
const ROW_WIDTH = 32;
const CHUNKS = TARGET.match(new RegExp(`.{1,${ROW_WIDTH}}`, "g"))!;

const ESC = "\x1b";
const BEL = "\x07";
const osc8 = (id: string, uri: string, visible: string) =>
  `${ESC}]8;id=${id};${uri}${BEL}${visible}${ESC}]8;;${BEL}`;

// Claude's login screen: alternate buffer + SGR mouse tracking, a header, then the URL rows.
const CLAUDE_MODES = `${ESC}[?1049h${ESC}[?1000h${ESC}[?1006h${ESC}[H${ESC}[2J`;
const loginScreen = () =>
  CLAUDE_MODES +
  "Browser didn't open? Use the url below to sign in\r\n\r\n" +
  CHUNKS.map((c) => `${ESC}[2C${osc8("15azii9", TARGET, c)}\r${ESC}[1B`).join("") +
  "\r\nPaste code here if prompted >";

// One OSC 8 link per screen whose visible text differs from its target.
const singleLink = (uri: string, visible: string) =>
  `${CLAUDE_MODES}header\r\n\r\n${ESC}[2C${osc8("one", uri, visible)}\r\n`;

const stub = (bytes: string) => `
window.__opened = [];
window.open = (...args) => { window.__opened.push(args); return null; };
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => {
      this.readyState = 1;
      this.onopen && this.onopen();
      this.onmessage && this.onmessage({ data: JSON.stringify({ t: "role", role: "owner" }) });
      const buf = new TextEncoder().encode(${JSON.stringify(bytes)}).buffer;
      this.onmessage && this.onmessage({ data: buf });
      this.onmessage && this.onmessage({ data: JSON.stringify({ t: "seq", n: buf.byteLength }) });
    }, 30);
  }
  send() {}
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

type DialogLog = { messages: string[] };

/** Load a session whose PTY prints `bytes`, answering every confirm() with `answer`. */
async function attach(
  page: Page,
  slug: string,
  bytes: string,
  waitFor: string,
  answer: "accept" | "dismiss",
): Promise<DialogLog> {
  const log: DialogLog = { messages: [] };
  page.on("dialog", (d) => {
    log.messages.push(`${d.type()}:${d.message()}`);
    void (answer === "accept" ? d.accept() : d.dismiss());
  });
  await page.addInitScript(stub(bytes));
  await page.goto(`/s/claude/${slug}`);
  await expect(page.locator(".xterm")).toBeVisible();
  await expect
    .poll(() => page.locator(".xterm-rows").innerText(), { timeout: 10_000 })
    .toContain(waitFor);
  return log;
}

/** Viewport point of the middle of `text` on the rendered row that contains it. */
async function pointOf(page: Page, text: string): Promise<{ x: number; y: number }> {
  const pt = await page.evaluate((needle) => {
    for (const row of Array.from(document.querySelectorAll(".xterm-rows > div"))) {
      const full = row.textContent ?? "";
      const at = full.indexOf(needle);
      if (at < 0) continue;
      const target = at + Math.floor(needle.length / 2);
      // Walk the row's text nodes to the character, then measure it with a Range.
      const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
      let seen = 0;
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const len = n.textContent?.length ?? 0;
        if (target < seen + len) {
          const r = document.createRange();
          r.setStart(n, target - seen);
          r.setEnd(n, target - seen + 1);
          const b = r.getBoundingClientRect();
          return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
        }
        seen += len;
      }
    }
    return null;
  }, text);
  if (!pt) throw new Error(`"${text}" is not on screen`);
  return pt;
}

/** Tap (mobile: touch on the capture overlay) or click (desktop) the middle of `text`. */
async function activate(page: Page, mobile: boolean, text: string): Promise<void> {
  const { x, y } = await pointOf(page, text);
  if (!mobile) {
    // xterm's linkifier resolves the link on hover; the click activates it.
    await page.mouse.move(x, y);
    await page.waitForTimeout(150);
    await page.mouse.click(x, y);
    return;
  }
  await page.locator("[data-touch-surface]").evaluate(
    (el, p) => {
      const touch = new Touch({ identifier: 1, target: el, clientX: p.x, clientY: p.y });
      el.dispatchEvent(
        new TouchEvent("touchstart", { cancelable: true, bubbles: true, touches: [touch] }),
      );
      el.dispatchEvent(
        new TouchEvent("touchend", { cancelable: true, bubbles: true, touches: [] }),
      );
    },
    { x, y },
  );
}

const opened = (page: Page) =>
  page.evaluate(() => (window as unknown as { __opened: unknown[][] }).__opened);

/** Give a would-be open (or confirm) time to happen before asserting it did not. */
const settle = (page: Page) => page.waitForTimeout(600);

for (const [label, chunk] of [
  ["the first row", CHUNKS[0]],
  ["a middle row", CHUNKS[Math.floor(CHUNKS.length / 2)]],
] as const) {
  test(`#2024: activating ${label} of Claude's OSC 8 login link offers and opens the FULL target`, async ({
    page,
  }, testInfo) => {
    const mobile = testInfo.project.name === "mobile";
    const log = await attach(page, `osc8-accept-${label.length}`, loginScreen(), CHUNKS[1], "accept");

    await activate(page, mobile, chunk);

    await expect.poll(() => log.messages.length).toBe(1);
    expect(log.messages[0]).toMatch(/^confirm:/);
    expect(log.messages[0]).toContain(TARGET);
    // Accepted → the one terminal-link opener (#1232): a new tab, no opener, no Referer.
    await expect.poll(() => opened(page)).toEqual([[TARGET, "_blank", "noopener,noreferrer"]]);
  });
}

// Hermes on PR #2030: xterm 5.5's own OSC 8 provider drops a link whose first cell is the row's
// LAST occupied cell, so a hard-wrapped URL whose final fragment is ONE character had no target
// on that row. The same screen, with a one-character last row.
const loginScreenOneCharTail = () =>
  CLAUDE_MODES +
  "Browser didn't open? Use the url below to sign in\r\n\r\n" +
  CHUNKS.map((c) => `${ESC}[2C${osc8("tail", TARGET, c)}\r${ESC}[1B`).join("") +
  `${ESC}[2C${osc8("tail", TARGET, "Z")}\r${ESC}[1B` +
  "\r\nPaste code here if prompted >";

test("#2024: a ONE-character final row of the login link offers and opens the FULL target", async ({
  page,
}, testInfo) => {
  expect(CHUNKS.join("")).not.toContain("Z"); // the needle is unique on screen
  const mobile = testInfo.project.name === "mobile";
  const log = await attach(page, "osc8-one-char-tail", loginScreenOneCharTail(), "Paste code", "accept");

  await activate(page, mobile, "Z");

  await expect.poll(() => log.messages.length).toBe(1);
  expect(log.messages[0]).toMatch(/^confirm:/);
  expect(log.messages[0]).toContain(TARGET);
  await expect.poll(() => opened(page)).toEqual([[TARGET, "_blank", "noopener,noreferrer"]]);
});

test("#2024: cancelling the confirm opens nothing — not the full target, not the visible fragment", async ({
  page,
}, testInfo) => {
  const mobile = testInfo.project.name === "mobile";
  const log = await attach(page, "osc8-cancel", loginScreen(), CHUNKS[1], "dismiss");

  await activate(page, mobile, CHUNKS[0]);

  await expect.poll(() => log.messages.length).toBe(1);
  expect(log.messages[0]).toContain(TARGET);
  await settle(page);
  expect(await opened(page)).toEqual([]);
});

test("#2024: a link whose visible text is a different URL confirms the ACTUAL destination", async ({
  page,
}, testInfo) => {
  const mobile = testInfo.project.name === "mobile";
  const visible = "https://docs.example/login";
  const target = "https://evil.example/?next=https://docs.example/login";
  const log = await attach(page, "osc8-spoof", singleLink(target, visible), visible, "dismiss");

  await activate(page, mobile, visible);

  await expect.poll(() => log.messages.length).toBe(1);
  expect(log.messages[0]).toContain(target);
  await settle(page);
  expect(await opened(page)).toEqual([]);
});

for (const [kind, target] of [
  ["a non-http(s) scheme", "javascript:alert(document.domain)"],
  ["a malformed target", "http://[oops"],
] as const) {
  test(`#2024: an OSC 8 link with ${kind} is refused — no confirm, nothing opened, no fragment fallback`, async ({
    page,
  }, testInfo) => {
    const mobile = testInfo.project.name === "mobile";
    // The visible text is itself a plain URL, so a fallback to the fragment would open it.
    const visible = "https://docs.example/visible";
    const log = await attach(page, `osc8-refuse-${kind.length}`, singleLink(target, visible), visible, "accept");

    await activate(page, mobile, visible);

    await settle(page);
    expect(log.messages).toEqual([]);
    expect(await opened(page)).toEqual([]);
  });
}

test("#2024: screenshot of the confirm showing the full target (PR evidence)", async ({
  page,
}, testInfo) => {
  const dir = process.env.OSC8_SHOTS_DIR;
  test.skip(!dir, "evidence capture only — set OSC8_SHOTS_DIR");
  const mobile = testInfo.project.name === "mobile";
  await attach(page, "osc8-shot", loginScreen(), CHUNKS[1], "dismiss");
  // A native confirm() is not part of the page, so screenshots cannot show it. Render its exact
  // text in an overlay over the terminal at the moment it is asked, then capture that.
  await page.evaluate(() => {
    window.confirm = (msg?: string) => {
      const box = document.createElement("div");
      box.id = "confirm-evidence";
      box.style.cssText =
        "position:fixed;left:50%;top:30%;transform:translateX(-50%);z-index:99999;width:min(92vw,560px);" +
        "background:#fff;color:#111;font:14px system-ui;padding:16px;border-radius:8px;" +
        "box-shadow:0 8px 32px rgba(0,0,0,.5);white-space:pre-wrap;word-break:break-all";
      box.textContent = `[native confirm()]\n\n${msg}\n\n[Cancel]   [OK]`;
      document.body.appendChild(box);
      return false;
    };
  });
  await activate(page, mobile, CHUNKS[Math.floor(CHUNKS.length / 2)]);
  await expect(page.locator("#confirm-evidence")).toContainText(TARGET);
  await page.screenshot({ path: `${dir}/osc8-confirm-${testInfo.project.name}.png` });
});
