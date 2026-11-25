import { expect, test, type Page } from "@playwright/test";

// #1285: codex ≥ 0.160 arms any-motion mouse tracking (?1003h + SGR ?1006h). xterm.js then
// reports EVERY pointer move as `ESC [< 35 ; x ; y M`; dtach forwards input in 8-byte packets,
// and a report split after its ESC reaches codex as an Esc keypress — interrupting the turn and
// typing `[<35;51;59M` into the composer. The fix drops hover-only reports in the browser, so
// this asserts on what actually leaves for the socket: no hover reports, while a click, a wheel
// notch (desktop) and a touch swipe (mobile, which has no hover) still deliver theirs.
//
// The stub models the #397 attach replay of codex's modes, then records every frame sent.
const STUB = `
window.__sent = [];
window.WebSocket = class {
  constructor(url) { this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    const enc = new TextEncoder();
    setTimeout(() => {
      this.readyState = 1;
      this.onopen && this.onopen();
      this.onmessage && this.onmessage({ data: JSON.stringify({ t: "role", role: "owner" }) });
      const modes = "\\x1b[?1000h\\x1b[?1002h\\x1b[?1003h\\x1b[?1006h\\x1b[?2004h";
      const bytes = enc.encode(modes + "codex TUI live frame\\r\\n");
      this.onmessage && this.onmessage({ data: bytes.buffer });
      this.onmessage && this.onmessage({ data: JSON.stringify({ t: "seq", n: bytes.length }) });
    }, 20);
  }
  send(d) { window.__sent.push(String(d)); }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

/** SGR button codes of every mouse report the app sent to the PTY, with their final byte. */
async function sentReports(page: Page): Promise<string[]> {
  const frames = await page.evaluate(
    () => (window as unknown as { __sent: string[] }).__sent,
  );
  const out: string[] = [];
  for (const f of frames) {
    let msg: { t?: string; d?: string };
    try {
      msg = JSON.parse(f);
    } catch {
      continue;
    }
    if (msg.t !== "i" || typeof msg.d !== "string") continue;
    // eslint-disable-next-line no-control-regex -- matching the literal ESC in SGR mouse reports
    for (const m of msg.d.matchAll(/\x1b\[<(\d+);\d+;\d+([Mm])/g))
      out.push(`${m[1]}${m[2]}`);
  }
  return out;
}

const isHover = (r: string) => {
  const b = Number.parseInt(r, 10);
  return r.endsWith("M") && b < 64 && (b & ~(4 | 8 | 16)) === 35;
};

async function attach(page: Page, sid: string) {
  await page.addInitScript(STUB);
  await page.goto(`/s/codex/${sid}`);
  await expect(page.locator(".xterm")).toBeVisible();
  // Sends before readyState 1 are dropped — wait for the connect-time resize frame.
  await page.waitForFunction(
    () =>
      ((window as unknown as { __sent?: unknown[] }).__sent?.length ?? 0) > 0,
  );
  const box = await page.locator(".xterm-screen").boundingBox();
  if (!box) throw new Error("no terminal screen");
  return box;
}

test("hovering a mouse-tracking pane sends no hover reports; click + wheel still do (#1285)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "mobile has no hover or wheel");
  const box = await attach(page, "hover-desktop");
  // Sweep the pointer across the pane — every step is an any-motion report to xterm.
  for (let i = 0; i < 12; i++)
    await page.mouse.move(box.x + 20 + i * 15, box.y + 20 + i * 6);
  await page.waitForTimeout(100);
  expect((await sentReports(page)).filter(isHover)).toEqual([]);

  // A click still reaches the TUI as a press/release pair …
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect
    .poll(
      async () =>
        (await sentReports(page)).filter((r) => r.endsWith("m")).length,
    )
    .toBeGreaterThan(0);
  expect((await sentReports(page)).some((r) => r === "0M")).toBe(true);

  // … and a wheel notch as a wheel report.
  await page.mouse.wheel(0, 120);
  await expect
    .poll(async () => (await sentReports(page)).some((r) => r.startsWith("65")))
    .toBe(true);
  expect((await sentReports(page)).filter(isHover)).toEqual([]);
});

test("a touch swipe on a mouse-tracking pane still forwards wheel reports (#1285, mobile)", async ({
  page,
}, testInfo) => {
  // Mobile has no hover and a tap opens the keyboard rather than clicking, so its only mouse
  // path is touch scroll forwarded as wheel reports (#559) — pin that it survives the filter.
  test.skip(testInfo.project.name !== "mobile", "touch-only regression");
  await attach(page, "hover-mobile");
  const surface = page.locator("[data-touch-surface]");
  await expect(surface).toBeVisible();
  await surface.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const cx = Math.round(r.x + r.width / 2);
    const touch = (y: number) =>
      new Touch({
        identifier: 1,
        target: el,
        clientX: cx,
        clientY: Math.round(y),
      });
    const fire = (type: string, y: number) =>
      el.dispatchEvent(
        new TouchEvent(type, {
          cancelable: true,
          bubbles: true,
          touches: type === "touchend" ? [] : [touch(y)],
        }),
      );
    let y = r.y + r.height * 0.25;
    fire("touchstart", y);
    for (let i = 0; i < 12; i++) {
      y += r.height * 0.05;
      fire("touchmove", y);
    }
    fire("touchend", y);
  });
  await expect
    .poll(async () => (await sentReports(page)).some((r) => /^6[45]M$/.test(r)))
    .toBe(true);
  expect((await sentReports(page)).filter(isHover)).toEqual([]);
});
