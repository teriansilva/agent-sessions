import { expect, test, type Page } from "@playwright/test";

// #1570: the uplink veil over the terminal while a session is slow to connect (> 3 s) or has lost
// its link (> 5 s), never for a shorter blip. Real browser, desktop + mobile projects: layering,
// pointer blocking on a touch device, motion modes and focused-keyboard behaviour are exactly what
// a DOM emulator cannot show.
//
// Red before #1570: there is no veil at all — every "appears" assertion below fails on main.

// A WebSocket the test drives from the page: `__ws.mode` decides whether a NEW socket opens
// ("open", after `delay` ms) or hangs in CONNECTING ("hold") until `__wsOpenHeld()`; `__wsDrop()`
// kills every open socket like a lost link (1006). Each socket records the input frames it sent.
// `__veil` logs every phase the veil shows, with the page's clock.
const HARNESS = `
window.__ws = { mode: "open", delay: 20, socks: [] };
window.__veil = { log: [], first: null };
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "arraybuffer"; this.input = [];
    window.__ws.socks.push(this);
    if (window.__ws.mode === "open") setTimeout(() => this._open(), window.__ws.delay);
    // "flap": accepted, then refused 50 ms later (a busy or restarting server).
    if (window.__ws.mode === "flap") setTimeout(() => { this._open(); setTimeout(() => window.__wsDrop(), 50); }, 20);
  }
  _open() {
    if (this.readyState !== 0) return;
    this.readyState = 1;
    this.onopen && this.onopen();
    this.onmessage && this.onmessage({ data: JSON.stringify({ t: "seq", n: 0 }) });
  }
  send(d) { try { const m = JSON.parse(d); if (m.t === "i") this.input.push(m.d); } catch {} }
  close() {
    if (this.readyState === 3) return;
    this.readyState = 3;
    setTimeout(() => this.onclose && this.onclose({ code: 1006, reason: "" }), 0);
  }
};
window.WebSocket.OPEN = 1;
window.__wsOpenHeld = () => { for (const s of window.__ws.socks) s._open(); };
window.__wsDrop = () => {
  for (const s of window.__ws.socks)
    if (s.readyState === 1) { s.readyState = 3; s.onclose && s.onclose({ code: 1006, reason: "" }); }
};
new MutationObserver(() => {
  const v = document.querySelector('[data-testid="uplink-veil"]');
  const phase = v ? v.getAttribute("data-phase") : null;
  const last = window.__veil.log.at(-1);
  if ((last ? last.phase : null) !== phase) {
    window.__veil.log.push({ phase, t: performance.now() });
    if (phase && window.__veil.first === null) window.__veil.first = performance.now();
  }
}).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-phase"] });
`;

async function open(page: Page, opts: { mode?: "open" | "hold"; delay?: number; motion?: string; theme?: string } = {}) {
  await page.addInitScript(
    ({ harness, mode, delay, motion, theme }) => {
      if (motion) localStorage.setItem("tr-motion", motion);
      if (theme) localStorage.setItem("tr-theme", theme);
      new Function(harness)();
      const w = window as unknown as { __ws: { mode: string; delay: number } };
      w.__ws.mode = mode;
      w.__ws.delay = delay;
    },
    { harness: HARNESS, mode: opts.mode ?? "open", delay: opts.delay ?? 20, motion: opts.motion, theme: opts.theme },
  );
  const t0 = Date.now();
  await page.goto("/s/claude/uplink-veil");
  await expect(page.locator(".xterm")).toBeVisible();
  return t0;
}

const veil = (page: Page) => page.getByTestId("uplink-veil");
const veilLog = (page: Page) =>
  page.evaluate(() => (window as unknown as { __veil: { log: { phase: string | null }[] } }).__veil.log.map((e) => e.phase));
const drop = (page: Page, mode: "open" | "hold" | "flap") =>
  page.evaluate((m) => {
    const w = window as unknown as { __ws: { mode: string }; __wsDrop: () => void };
    w.__ws.mode = m;
    w.__wsDrop();
  }, mode);
const openHeld = (page: Page) =>
  page.evaluate(() => {
    const w = window as unknown as { __ws: { mode: string }; __wsOpenHeld: () => void };
    w.__ws.mode = "open";
    w.__wsOpenHeld();
  });
const connected = (page: Page) =>
  expect.poll(() =>
    page.evaluate(() => (window as unknown as { __ws: { socks: { readyState: number }[] } }).__ws.socks.some((s) => s.readyState === 1)),
  ).toBe(true);
/** Connected AND held past the veil's 750 ms STABLE_MS, so a following drop is a real drop of a
 *  settled link (a link that closes sooner is a flap of the same outage, by design). */
/** Drive the tab's visibility. Headless Chromium keeps every page "visible", so this goes through
 *  the document's own property plus the real visibilitychange event (what the app listens to).
 *  Returns the page's clock at the change. */
const setVis = (page: Page, v: "hidden" | "visible") =>
  page.evaluate((state) => {
    const w = window as unknown as { __vis?: string };
    if (w.__vis === undefined)
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => w.__vis });
    w.__vis = state;
    document.dispatchEvent(new Event("visibilitychange"));
    return performance.now();
  }, v);
const settled = async (page: Page) => {
  await connected(page);
  await page.waitForTimeout(900);
};

test.describe("uplink veil (#1570)", () => {
  test("a connect that opens inside 3 s shows no veil", async ({ page }) => {
    await open(page, { delay: 2000 });
    await connected(page);
    await page.waitForTimeout(2500); // well past the 3 s mark from the mount
    expect(await veilLog(page)).toEqual([]);
  });

  test("a held connect shows ESTABLISHING at ~3 s, then RESTORED, then the veil is gone", async ({ page }) => {
    const t0 = await open(page, { mode: "hold" });
    await expect(veil(page)).toHaveAttribute("data-phase", "establishing", { timeout: 6000 });
    expect(Date.now() - t0).toBeGreaterThanOrEqual(2900);
    await expect(veil(page)).toContainText(/uplink \/\/ establishing/i);
    await expect(veil(page)).toContainText(/attempt \/\/ \d/i);
    await openHeld(page);
    await expect(veil(page)).toHaveAttribute("data-phase", "restored");
    await expect(veil(page)).toHaveCount(0, { timeout: 3000 });
  });

  test("a drop that heals inside 5 s shows nothing", async ({ page }) => {
    await open(page);
    await settled(page);
    await drop(page, "hold");
    await page.waitForTimeout(2000);
    await openHeld(page);
    await connected(page);
    await page.waitForTimeout(4500); // past where a 5 s veil would have appeared
    expect(await veilLog(page)).toEqual([]);
  });

  test("a 7 s drop shows RE-ESTABLISHING with a live clock, then RESTORED, then nothing", async ({ page }) => {
    await open(page);
    await settled(page);
    const dropAt = Date.now();
    await drop(page, "hold");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 7000 });
    expect(Date.now() - dropAt).toBeGreaterThanOrEqual(4900);
    await expect(veil(page)).toContainText(/link lost \/\/ 00:0[5-6]/i);
    await expect(page.getByTestId("uplink-elapsed")).toHaveText(/00:0[6-7]/, { timeout: 2500 });
    await openHeld(page);
    await expect(veil(page)).toHaveAttribute("data-phase", "restored");
    await expect(veil(page)).toContainText(/down for \/\/ 00:0[6-9]/i);
    await expect(veil(page)).toHaveCount(0, { timeout: 3000 });
    expect(await veilLog(page)).toEqual(["lost", "restored", null]);
  });

  test("a flapping link (each retry accepted, then refused) still shows the veil, counting tries", async ({ page }) => {
    await open(page);
    await settled(page);
    const dropAt = Date.now();
    await drop(page, "flap");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 7000 });
    expect(Date.now() - dropAt).toBeGreaterThanOrEqual(4900);
    // Several retries opened in those 5 s; the count climbed instead of reading the socket's reset "1".
    await expect(veil(page)).toContainText(/attempt \/\/ ([3-9]|\d\d)/i);
    await page.waitForTimeout(1500); // more flaps: the veil stays, no RESTORED
    expect(await veilLog(page)).toEqual(["lost"]);
  });

  test("a link lost behind a hidden tab: no veil while hidden or at the instant of return, then the 1 s grace", async ({ page }) => {
    await open(page);
    await settled(page);
    await setVis(page, "hidden");
    await drop(page, "hold");
    await page.waitForTimeout(6000); // past the 5 s grace, in the background
    expect(await veilLog(page)).toEqual([]);
    const back = await setVis(page, "visible");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 4000 });
    const first = await page.evaluate(() => (window as unknown as { __veil: { first: number } }).__veil.first);
    expect(first - back).toBeGreaterThanOrEqual(950); // not on screen at the return: the grace ran first
  });

  test("a veil already on screen comes down while hidden; the return waits out the 1 s grace (same outage)", async ({ page }) => {
    await open(page);
    await settled(page);
    await drop(page, "hold");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 7000 });
    await setVis(page, "hidden");
    await expect(veil(page)).toHaveCount(0);
    await page.waitForTimeout(2000);
    const back = await setVis(page, "visible");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 4000 });
    const reshown = await page.evaluate(
      () => (window as unknown as { __veil: { log: { phase: string | null; t: number }[] } }).__veil.log.at(-1)!.t,
    );
    expect(reshown - back).toBeGreaterThanOrEqual(950);
    // The same outage: the clock kept counting from the original drop (5 s + 2 s hidden + 1 s grace).
    await expect(page.getByTestId("uplink-elapsed")).toHaveText(/00:0[7-9]/);
  });

  test("hidden again inside the return grace: never mounted while hidden, a full grace on the next return", async ({ page }) => {
    await open(page);
    await settled(page);
    await drop(page, "hold");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 7000 });
    await setVis(page, "hidden");
    await page.waitForTimeout(1500);
    await setVis(page, "visible");
    await page.waitForTimeout(400); // inside the return grace…
    await setVis(page, "hidden"); // …hidden again
    const hiddenAt = await page.evaluate(() => performance.now());
    await page.waitForTimeout(2000); // the first return's grace timer has fired by now
    const whileHidden = await page.evaluate(
      (since) =>
        (window as unknown as { __veil: { log: { phase: string | null; t: number }[] } }).__veil.log.filter(
          (e) => e.t > since && e.phase !== null,
        ).length,
      hiddenAt,
    );
    expect(whileHidden).toBe(0); // nothing mounted behind the hidden tab
    await expect(veil(page)).toHaveCount(0);
    const back = await setVis(page, "visible");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 4000 });
    const reshown = await page.evaluate(
      () => (window as unknown as { __veil: { log: { phase: string | null; t: number }[] } }).__veil.log.at(-1)!.t,
    );
    expect(reshown - back).toBeGreaterThanOrEqual(950);
  });

  test("a FAST second hide (back before the first return's timer fires) still gets a full grace", async ({ page }) => {
    await open(page);
    await settled(page);
    await drop(page, "hold");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 7000 });
    await setVis(page, "hidden");
    await page.waitForTimeout(1500);
    await setVis(page, "visible");
    await page.waitForTimeout(400);
    await setVis(page, "hidden");
    await page.waitForTimeout(200); // back before the first return's 1 s timer has fired
    const back = await setVis(page, "visible");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 4000 });
    const reshown = await page.evaluate(
      () => (window as unknown as { __veil: { log: { phase: string | null; t: number }[] } }).__veil.log.at(-1)!.t,
    );
    expect(reshown - back).toBeGreaterThanOrEqual(950); // not the first return's stale deadline
  });

  test("with xterm focused, keys are not sent while down and reach the new socket during RESTORED", async ({ page }) => {
    await open(page);
    await settled(page);
    // Focus xterm's own input (a click would land on the touch layer on a phone).
    await page.locator(".xterm-helper-textarea").focus();
    await drop(page, "hold");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 7000 });
    await page.keyboard.type("lost");
    await openHeld(page);
    await expect(veil(page)).toHaveAttribute("data-phase", "restored");
    await page.keyboard.type("back");
    const inputs = await page.evaluate(() =>
      (window as unknown as { __ws: { socks: { input: string[] }[] } }).__ws.socks.map((s) => s.input.join("")),
    );
    expect(inputs.join("|")).not.toContain("lost");
    expect(inputs.at(-1)).toContain("back");
  });

  test("the veil sits above every pane overlay and swallows a tap (phone)", async ({ page }, info) => {
    test.skip(info.project.name !== "mobile", "the touch-capture layer exists on coarse pointers only");
    await open(page);
    await settled(page);
    await drop(page, "hold");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 7000 });
    await page.evaluate(() => {
      const w = window as unknown as { __touchHits: number };
      w.__touchHits = 0;
      document.querySelector("[data-touch-surface]")?.addEventListener("pointerdown", () => (w.__touchHits += 1));
    });
    const box = await veil(page).boundingBox();
    if (!box) throw new Error("veil has no box");
    await page.touchscreen.tap(box.x + 10, box.y + box.height - 10);
    expect(await page.evaluate(() => (window as unknown as { __touchHits: number }).__touchHits)).toBe(0);
    const top = await page.evaluate(() => {
      const v = document.querySelector('[data-testid="uplink-veil"]') as HTMLElement;
      const r = v.getBoundingClientRect();
      return document.elementFromPoint(r.left + 5, r.bottom - 5)?.closest('[data-testid="uplink-veil"]') !== null;
    });
    expect(top).toBe(true);
    // ≤800 px: no backdrop-filter (mobile Chrome may not paint it); the tint carries it.
    expect(await veil(page).evaluate((el) => getComputedStyle(el).backdropFilter)).toBe("none");
  });

  test("desktop: the terminal behind recedes — blurred and tinted toward --ground", async ({ page }, info) => {
    test.skip(info.project.name !== "desktop", "≤800 px drops the blur by design (asserted on the phone)");
    await open(page);
    await settled(page);
    await drop(page, "hold");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 7000 });
    const look = await veil(page).evaluate((el) => {
      const cs = getComputedStyle(el);
      return { blur: cs.backdropFilter, bg: cs.backgroundColor };
    });
    expect(look.blur).toContain("blur(3px)"); // the BUILT css must carry it (a -webkit-only rule did not)
    expect(look.bg).toMatch(/\/ 0\.48\)$|, 0\.48\)$/);
  });

  test("motion Off: the veil appears and leaves with no animation", async ({ page }) => {
    await open(page, { mode: "hold", motion: "off" });
    await expect(veil(page)).toHaveAttribute("data-phase", "establishing", { timeout: 6000 });
    const running = await veil(page).evaluate((el) =>
      [el, ...el.querySelectorAll("*")].flatMap((n) => n.getAnimations()).length,
    );
    expect(running).toBe(0);
    await openHeld(page);
    await expect(veil(page)).toHaveCount(0, { timeout: 3000 });
  });

  test("motion Full: the veil fades in over --t-slow (320 ms)", async ({ page }) => {
    // Full first.
    await open(page, { mode: "hold", motion: "full" });
    await expect(veil(page)).toHaveAttribute("data-phase", "establishing", { timeout: 6000 });
    const fullIn = await veil(page).evaluate((el) =>
      el.getAnimations().map((a) => (a.effect as KeyframeEffect).getTiming().duration),
    );
    expect(fullIn).toEqual([320]);
  });

  test("motion Reduced: a 120 ms fade in, a 120 ms fade out on recovery, never stuck on screen", async ({ page }) => {
    await open(page, { mode: "hold", motion: "reduced" });
    await expect(veil(page)).toHaveAttribute("data-phase", "establishing", { timeout: 6000 });
    const enter = await veil(page).evaluate((el) => ({
      veil: el.getAnimations().map((a) => [(a as CSSAnimation).animationName, (a.effect as KeyframeEffect).getTiming().duration]),
      log: [...el.querySelectorAll("*")].flatMap((n) => n.getAnimations()).length,
    }));
    expect(enter.veil).toEqual([["nj-fade-in", 120]]);
    expect(enter.log).toBe(0); // no rise, no scan loop
    // Record every animation that STARTS on the veil from here on: the exit lives ~120 ms, too
    // short to catch by polling.
    await page.evaluate(() => {
      const w = window as unknown as { __veilAnims: [string, string][] };
      w.__veilAnims = [];
      document.addEventListener(
        "animationstart",
        (e) => {
          const el = e.target as HTMLElement;
          if (el.dataset?.testid === "uplink-veil")
            w.__veilAnims.push([e.animationName, getComputedStyle(el).animationDuration]);
        },
        true,
      );
    });
    await openHeld(page);
    await expect(veil(page)).toHaveAttribute("data-phase", "restored");
    await expect(veil(page)).toHaveCount(0, { timeout: 4000 });
    expect(
      await page.evaluate(() => (window as unknown as { __veilAnims: [string, string][] }).__veilAnims),
    ).toEqual([["nj-fade-out", "0.12s"]]);
  });

  test("the ticking clock is readable but only the heading is announced", async ({ page }) => {
    await open(page);
    await settled(page);
    await drop(page, "hold");
    await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 7000 });
    const live = await veil(page).evaluate((el) => ({
      regions: [el, ...el.querySelectorAll("*")]
        .filter((n) => n.getAttribute("aria-live") || n.getAttribute("role") === "status")
        .map((n) => n.getAttribute("data-testid")),
      clockInLive: !!document.querySelector('[data-testid="uplink-elapsed"]')?.closest('[aria-live]:not([aria-live="off"]), [role="status"]'),
    }));
    expect(live.regions).toEqual(["uplink-head"]);
    expect(live.clockInLive).toBe(false);
    await expect(page.getByTestId("uplink-elapsed")).toBeVisible();
    await expect(page.getByTestId("uplink-head")).toHaveText(/uplink \/\/ re-establishing/i);
    // The pane holds ONE live region while the veil is up: the corner chip stands down.
    const paneLive = await page.evaluate(() => {
      const area = document.querySelector('[data-testid="uplink-veil"]')!.parentElement!;
      return [...area.querySelectorAll('[role="status"], [aria-live]:not([aria-live="off"])')].map(
        (n) => n.getAttribute("data-testid"),
      );
    });
    expect(paneLive).toEqual(["uplink-head"]);
    // aria-busy marks the terminal content, never an ancestor of the live heading — assistive tech
    // may defer a busy subtree's announcements until busy clears, i.e. until the link is back.
    const busy = await page.evaluate(() => ({
      overHead: document.querySelector('[data-testid="uplink-head"]')!.closest('[aria-busy="true"]') !== null,
      onTerm: document.querySelector(".xterm")!.closest('[aria-busy="true"]') !== null,
    }));
    expect(busy).toEqual({ overHead: false, onTerm: true });
    // The heading node is stable while the clock ticks: no mutation inside the live region until
    // the phase changes, and then the SAME node carries the new phase.
    await page.evaluate(() => {
      const head = document.querySelector('[data-testid="uplink-head"]')!;
      const w = window as unknown as { __head: Element; __headMut: number };
      w.__head = head;
      w.__headMut = 0;
      new MutationObserver((m) => (w.__headMut += m.length)).observe(head, {
        subtree: true, childList: true, characterData: true, attributes: true,
      });
    });
    await expect(page.getByTestId("uplink-elapsed")).not.toHaveText(
      await page.getByTestId("uplink-elapsed").innerText(),
      { timeout: 2500 },
    ); // the clock moved on…
    expect(await page.evaluate(() => (window as unknown as { __headMut: number }).__headMut)).toBe(0); // …the heading did not
    await openHeld(page);
    await expect(page.getByTestId("uplink-head")).toHaveText(/uplink \/\/ restored/i);
    expect(
      await page.evaluate(
        () => (window as unknown as { __head: Element }).__head === document.querySelector('[data-testid="uplink-head"]'),
      ),
    ).toBe(true);
  });

  for (const theme of ["dark", "light"] as const) {
    test(`captures the veil in ${theme} (PR screenshots)`, async ({ page }, info) => {
      await open(page, { theme });
      await settled(page);
      await drop(page, "hold");
      await expect(veil(page)).toHaveAttribute("data-phase", "lost", { timeout: 7000 });
      await page.waitForTimeout(600); // let the arrival fade finish
      await page.screenshot({ path: info.outputPath(`uplink-veil-${theme}-${info.project.name}.png`) });
    });
  }
});
