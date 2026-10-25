/** Calling HQ (#1397 P1) in a real browser, desktop and mobile, with Chromium's fake microphone.
 *
 *  The server is mocked: `/api/**` by route, the call socket by `page.routeWebSocket`, so each test
 *  plays the server's side of the protocol (`routes/voice.py`, `voice_calls.py`) and asserts what
 *  the page does — the microphone stays silent until `connected`, captions, an interrupt flush,
 *  the dropped state naming the endpoint, focus in and back out, 44 px controls, the on-call bar
 *  taking room instead of covering the pane, the unavailable states, and Settings → HQ calls. */
import { expect, test } from "@playwright/test";

import { AVAILABLE, CID, FAKE_MIC, connect, mockShell, say } from "./callFixture";
import { TOUCH_FLOOR } from "./touchTarget";

test.use(FAKE_MIC);

test("call from the card: Mic · not sending until connected, captions, interrupt, hang up returns focus", async ({ page }) => {
  const srv = await mockShell(page);
  const panel = await connect(page, srv);
  expect(JSON.parse(srv.texts[0][0])).toEqual({ t: "hello", csrf: "csrf-x" });
  expect(srv.calls[0]).toEqual({ contact: "asia-woman", conversation_id: null });
  await expect(page.getByTestId("call-mic")).toHaveText("Not sending");
  await expect(panel).toBeFocused();
  // Give the fake microphone time to produce frames: none may leave before `connected`.
  await page.waitForTimeout(600);
  expect(srv.binaries[0]).toBe(0);

  srv.connected[0] = true;
  say(srv, { t: "connected" });
  await expect(panel).toHaveAttribute("data-phase", "connected");
  await expect(page.getByTestId("call-mic")).toHaveText("Sending");
  await expect(page.getByTestId("call-live")).toHaveText("Connected to HQ, General Hana Seo.");
  await expect.poll(() => srv.binaries[0], { timeout: 10_000 }).toBeGreaterThan(5);
  expect(srv.binariesBeforeConnected[0]).toBe(0);

  say(srv, { t: "caption", who: "you", id: "u1", text: "How is api rebase?" });
  say(srv, { t: "caption", who: "hq", id: "h1", text: "Two auth tests fail.", kind: "answer", turn_id: "t1" });
  say(srv, { t: "turn", turn: { turn_id: "t1", status: "done" } });
  const caps = page.getByTestId("call-caption");
  await expect(caps).toHaveCount(2);
  await expect(caps.nth(1)).toHaveAttribute("data-status", "queued");
  say(srv, { t: "speaking", on: true });
  await expect(caps.nth(1)).toHaveAttribute("data-status", "speaking");
  // Speech audio arrives and plays; the interrupt flushes it and cuts the caption.
  srv.sockets[0].send(Buffer.alloc(9600));
  say(srv, { t: "interrupt" });
  await expect(caps.nth(1)).toHaveAttribute("data-status", "cut");
  // HQ's card shows in the call, confirmed by tap.
  await expect(page.getByTestId("call-cards").getByTestId("ask-nudge")).toBeVisible();
  // The live region never read a caption.
  await expect(page.getByTestId("call-live")).toHaveText("Connected to HQ, General Hana Seo.");

  // Every control is at least 44 px.
  for (const b of await panel.getByRole("button").all()) {
    const box = (await b.boundingBox())!;
    expect(box.height, await b.getAttribute("data-testid") ?? "").toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(box.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }

  await page.getByTestId("call-end").click();
  await expect(page.getByTestId("call-ended")).toHaveAttribute("data-reason", "hung_up");
  expect(srv.texts[0].map((t) => JSON.parse(t).t)).toContain("end");
  await expect(page.getByTestId("call-live")).toHaveText("Call ended.");
  await page.getByTestId("call-close").click();
  await expect(page.getByTestId("call-panel")).toHaveCount(0);
  await expect(page.getByTestId("hq-primary-call")).toBeFocused();
});

test("a dropped call names the endpoint, stops the mic, and only a tap calls again", async ({ page }) => {
  const srv = await mockShell(page);
  await connect(page, srv);
  srv.connected[0] = true;
  say(srv, { t: "connected" });
  await expect(page.getByTestId("call-panel")).toHaveAttribute("data-phase", "connected");
  say(srv, { t: "ended", reason: "tts_failed", detail: "gone", endpoint: "tts" });
  const ended = page.getByTestId("call-ended");
  await expect(ended).toHaveAttribute("data-reason", "tts_failed");
  await expect(page.getByTestId("call-ended-text")).toHaveText(
    "gpu-1.lan:8880 (text-to-speech) stopped answering.",
  );
  await expect(ended).toContainText("Mic off");
  // Nothing reconnects or sends audio on its own.
  const sent = srv.binaries[0];
  await page.waitForTimeout(800);
  expect(srv.sockets).toHaveLength(1);
  expect(srv.binaries[0]).toBe(sent);
  // Call again is a tap: a new admission into the same conversation, and a new socket.
  await page.getByTestId("call-again").click();
  await expect.poll(() => srv.sockets.length).toBe(2);
  expect(srv.calls[1]).toEqual({ contact: "asia-woman", conversation_id: CID });
});

test("minimised, the on-call bar takes room under the top bar and never covers the pane", async ({ page }) => {
  const srv = await mockShell(page);
  await connect(page, srv);
  srv.connected[0] = true;
  say(srv, { t: "connected" });
  await page.getByTestId("call-minimise").click();
  const bar = page.getByTestId("call-bar");
  await expect(bar).toBeVisible();
  await expect(page.getByTestId("call-panel")).toHaveCount(0);
  await expect(page.getByTestId("call-bar-open")).toBeFocused();
  const barBox = (await bar.boundingBox())!;
  const header = (await page.locator(".hud-topbar").boundingBox())!;
  const pane = (await page.locator(".terminal-pane").boundingBox())!;
  expect(barBox.y).toBeGreaterThanOrEqual(header.y + header.height - 1);
  expect(pane.y).toBeGreaterThanOrEqual(barBox.y + barBox.height - 1);
  for (const id of ["call-bar-mute", "call-bar-open", "call-bar-end"]) {
    const box = (await page.getByTestId(id).boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(box.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  // Opening it again brings the panel back, focused, the call still running.
  await page.getByTestId("call-bar-open").click();
  await expect(page.getByTestId("call-panel")).toBeFocused();
  await expect(page.getByTestId("call-panel")).toHaveAttribute("data-phase", "connected");
});

test("not set up: the card offers Set up calls; unavailable says why and starts nothing", async ({ page }) => {
  const srv = await mockShell(page, { ...AVAILABLE, configured: false, available: false, reason: "set up calls first" });
  await page.goto("/dashboard");
  const setup = page.getByTestId("hq-primary-call");
  await expect(setup).toHaveText(/Set up calls/);
  await expect(setup).toHaveAttribute("href", /\/settings\/ai-hq-calls$/);

  srv.caps = {
    ...AVAILABLE,
    available: false,
    reason: "calls over Home Free are not available yet — call from your local network",
  };
  await page.reload();
  const call = page.getByTestId("hq-primary-call");
  await expect(call).toHaveAttribute("aria-disabled", "true");
  await expect(page.getByTestId("hq-primary-call-why")).toContainText("Home Free");
  await call.click({ force: true });
  await page.waitForTimeout(300);
  expect(srv.calls).toHaveLength(0);
  await expect(page.getByTestId("call-panel")).toHaveCount(0);
});

test("Settings → HQ calls: a stored token reads Saved, Remove sends null, Test both runs the check", async ({ page }) => {
  const srv = await mockShell(page);
  const tests: unknown[] = [];
  await page.route("**/api/voice/test", (r) => {
    tests.push(r.request().postDataJSON());
    return r.fulfill({
      json: {
        usable: true,
        checks: [
          { id: "tts.synthesize", ok: true, required: true, note: "", value: null },
          { id: "tts.first_audio", ok: false, required: false, note: "first audio after 1.9 s: HQ will start speaking late", value: null },
        ],
      },
    });
  });
  await page.goto("/settings/ai-hq-calls");
  await expect(page.getByTestId("hq-calls-tts-token-state")).toHaveText("Saved");
  await page.getByTestId("hq-calls-test").click();
  await expect(page.getByTestId("hq-calls-report")).toContainText("HQ will start speaking late");
  expect(tests).toHaveLength(1);
  await page.getByTestId("hq-calls-tts-token-remove").click();
  await page.getByTestId("hq-calls-save").click();
  await expect.poll(() => srv.saves.length).toBe(1);
  expect((srv.saves[0] as { tts: { api_key: unknown } }).tts.api_key).toBeNull();
  await expect(page.getByTestId("hq-calls-destinations")).toContainText("ai.example.com");
  // No sideways scroll on either width.
  const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(over).toBeLessThanOrEqual(0);
});

test("on a phone, the navigation opens OVER the call panel and can be used (#1640 review)", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "the drawer is a phone's navigation");
  const srv = await mockShell(page);
  const panel = await connect(page, srv);
  srv.connected[0] = true;
  say(srv, { t: "connected" });
  await expect(panel).toHaveAttribute("data-phase", "connected");
  // The call follows the operator across routes; the sessions page has the drawer.
  await page.getByRole("link", { name: "Sessions", exact: true }).click();
  await expect(panel).toHaveAttribute("data-phase", "connected");
  await page.getByRole("button", { name: /Open session list/i }).click();
  const drawer = page.locator(".sidebar");
  await expect(drawer).toBeVisible();
  await expect.poll(async () => (await drawer.boundingBox())?.x ?? -1).toBeGreaterThanOrEqual(0); // slid in
  // The live call folded into its on-call bar: it goes on, out of the drawer's way.
  await expect(page.getByTestId("call-bar")).toBeVisible();
  // What a tap at the drawer's own controls hits is the drawer — never the call panel above it.
  const target = drawer.getByText(/New session/i).first();
  const box = (await target.boundingBox())!;
  const hit = await page.evaluate(
    ([x, y]) => {
      const el = document.elementFromPoint(x, y);
      return { inDrawer: !!el?.closest(".sidebar"), inPanel: !!el?.closest("[data-testid='call-panel']") };
    },
    [box.x + box.width / 2, box.y + box.height / 2],
  );
  expect(hit).toEqual({ inDrawer: true, inPanel: false });
});

for (const path of ["ended", "live"] as const) {
  test(`${path === "ended" ? "Continue in text" : "Type"} opens HQ on the call's conversation on HQ's first open (#1640 review 6907)`, async ({ page }) => {
    const srv = await mockShell(page);
    const panel = await connect(page, srv);
    srv.connected[0] = true;
    say(srv, { t: "connected" });
    await expect(panel).toHaveAttribute("data-phase", "connected");
    // HQ's console has not been opened yet in this page: it mounts after the request.
    await expect(page.getByTestId("ask-turn")).toHaveCount(0);
    if (path === "ended") {
      say(srv, { t: "ended", reason: "hung_up", detail: "", endpoint: "" });
      await page.getByTestId("call-continue-text").click();
    } else {
      await page.getByTestId("call-type").click();
      await expect(page.getByTestId("call-bar")).toBeVisible(); // the call goes on, minimised
    }
    // The call's thread, not an empty conversation.
    await expect(page.getByTestId("ask-turn").filter({ hasText: "How is api rebase?" })).toBeVisible();
  });
}

test("on a phone, a call started from HQ's drawer owns the keyboard; the drawer steps aside (#1640 review 6907)", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "HQ is a modal drawer on a phone");
  const srv = await mockShell(page);
  await page.goto("/dashboard");
  await page.getByTestId("ask-toggle").click();
  const call = page.getByTestId("ask-call");
  await expect(call).toBeVisible();
  await call.click();
  const panel = page.getByTestId("call-panel");
  await expect(panel).toBeVisible();
  await expect.poll(() => srv.texts[0]?.length ?? 0).toBeGreaterThan(0);
  srv.connected[0] = true;
  say(srv, { t: "connected" });
  await expect(panel).toHaveAttribute("data-phase", "connected");
  await expect(call).toBeHidden(); // HQ's drawer closed
  // Tab walks the call's own controls; nothing pulls focus back into HQ's drawer.
  const inPanel = () =>
    page.evaluate(() => !!document.activeElement?.closest("[data-testid='call-panel']"));
  await expect.poll(inPanel).toBe(true);
  for (let i = 0; i < 4; i++) {
    await page.keyboard.press("Tab");
    expect(await inPanel()).toBe(true);
  }
  // Type folds the call and opens HQ's drawer on its conversation; reopening the call from that
  // drawer's own button steps out of the drawer again (#1640 second review).
  await page.getByTestId("call-type").click();
  await expect(page.getByTestId("call-bar")).toBeVisible();
  await expect(call).toBeVisible();
  await call.click(); // now "Open the call with HQ"
  await expect(panel).toBeVisible();
  await expect(call).toBeHidden();
  await expect.poll(inPanel).toBe(true);
  for (let i = 0; i < 4; i++) {
    await page.keyboard.press("Tab");
    expect(await inPanel()).toBe(true);
  }
  // Hanging up and closing hands focus to HQ's corner icon, not the drawer's hidden button.
  await page.getByTestId("call-end").click();
  await page.getByTestId("call-close").click();
  await expect(page.getByTestId("ask-toggle")).toBeFocused();
});

test("a saved setup change shows on the Call buttons at once — no reload, no tab switch (#1640 review 6912)", async ({ page }) => {
  const srv = await mockShell(page, { ...AVAILABLE, configured: false, available: false, reason: "set up calls first" });
  await page.goto("/dashboard");
  await expect(page.getByTestId("hq-primary-call")).toHaveAttribute("data-call", "setup");
  await page.getByTestId("hq-primary-call").click(); // in-app navigation to Settings → HQ calls
  await expect(page.getByTestId("hq-calls-save")).toBeVisible();
  srv.caps = AVAILABLE; // what the server reports once these endpoints are stored
  await page.getByTestId("hq-calls-save").click();
  await expect.poll(() => srv.saves.length).toBe(1);
  await page.getByRole("link", { name: "Dashboard", exact: true }).click();
  await expect(page.getByTestId("hq-primary-call")).toHaveAttribute("data-call", "ready");
  // And back: a change that leaves calls unconfigured takes the Call away.
  await page.goto("/settings/ai-hq-calls");
  await expect(page.getByTestId("hq-calls-save")).toBeVisible();
  srv.caps = { ...AVAILABLE, configured: false, available: false, reason: "set up calls first" };
  await page.getByTestId("hq-calls-save").click();
  await expect.poll(() => srv.saves.length).toBe(2);
  await page.getByRole("link", { name: "Dashboard", exact: true }).click();
  await expect(page.getByTestId("hq-primary-call")).toHaveAttribute("data-call", "setup");
});

test("the fake microphone's signal reaches the server as sound, through the noise gate (#1667)", async ({ page }) => {
  const srv = await mockShell(page);
  await connect(page, srv);
  srv.connected[0] = true;
  say(srv, { t: "connected" });
  await expect.poll(() => srv.voiced[0], { timeout: 15_000 }).toBeGreaterThan(5);
});

test("HQ's speech plays in a 24 kHz audio context, so its chunks are never resampled one by one", async ({ page }) => {
  // At a 44.1 kHz device, 24 kHz chunks resampled one by one clicked at every 40 ms boundary
  // (heard as stutter). The call opens a playback context at the speech's own rate.
  await page.addInitScript(() => {
    const Real = window.AudioContext;
    const rates: number[] = [];
    (window as unknown as { __ctxRates: number[] }).__ctxRates = rates;
    window.AudioContext = class extends Real {
      constructor(opts?: AudioContextOptions) {
        super(opts);
        rates.push(this.sampleRate);
      }
    } as typeof AudioContext;
  });
  const srv = await mockShell(page);
  await connect(page, srv);
  srv.connected[0] = true;
  say(srv, { t: "connected" });
  const rates = () => page.evaluate(() => (window as unknown as { __ctxRates: number[] }).__ctxRates);
  await expect.poll(rates).toContain(24_000);
});
