import { expect, test } from "@playwright/test";

import { verifyHuman } from "./connect-helpers";
import { expectIdleBudget } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";

// #579 app-only connect: the public connect page no longer exposes the recovery terminal pane.
// Real browser guard because this is page structure/layout, not a jsdom-only contract.
test("Home Free connect page exposes the app root, not a recovery terminal pane", async ({
  page,
}) => {
  await page.goto("/connect.html");

  await expect(page.getByRole("button", { name: "CONNECT" })).toBeVisible();
  await expect(page.locator("#app-root")).toHaveCount(1);
  await expect(page.locator("#term")).toHaveCount(0);
  await expect(page.locator(".xterm")).toHaveCount(0);
});

/** Count rAF calls so "an animation loop is running / is not running" is observable, not inferred. */
async function countRaf(page: import("@playwright/test").Page): Promise<void> {
  await page.addInitScript(() => {
    (window as unknown as { __raf: number }).__raf = 0;
    const real = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (cb) => {
      (window as unknown as { __raf: number }).__raf++;
      return real(cb);
    };
  });
}
const rafCount = (page: import("@playwright/test").Page) =>
  page.evaluate(() => (window as unknown as { __raf: number }).__raf);

/** The atmosphere's endless CSS animations, by play state (the scan, the sweep). */
const atmoLoops = (page: import("@playwright/test").Page) =>
  page.locator(".nj-atmo").evaluate((el) =>
    el
      .getAnimations({ subtree: true })
      .filter((a) => a.effect?.getComputedTiming().iterations === Infinity)
      .map((a) => a.playState),
  );

// #1596: the connect page wears the app's own sheets (tokens, primitives, motion), not a copy, and
// the app's atmosphere in place of the retired data-flow canvas (docs/design.md §6/§8).
test("connect page wears NIGHTJAR: the app's atmosphere, chamfered card and gate, a glitchable CTA", async ({
  page,
}) => {
  // The atmosphere drifts on dark only (daylight is the still hex grid); headless defaults to light.
  await page.emulateMedia({ reducedMotion: "no-preference", colorScheme: "dark" });
  await page.goto("/connect.html");

  // The app's palette is in force, read from the app's own tokens.css.
  const hud = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--hud").trim());
  expect(hud).not.toBe("");

  // The atmosphere paints behind everything, never eats a click, and drifts in Full.
  const atmo = page.locator(".nj-atmo");
  await expect(atmo).toHaveCount(1);
  await expect(atmo).toHaveCSS("pointer-events", "none");
  await expect(atmo.locator(".nj-hex")).toBeVisible();
  await expect(page.locator("canvas")).toHaveCount(0);
  await expect.poll(() => atmoLoops(page)).toContain("running");

  // THE panel: a chamfered frame with its rail, not a rounded box and not corner brackets.
  const card = page.locator(".connect-card");
  await expect(card).toHaveClass(/\bnj-frame\b/);
  await expect(card).not.toHaveCSS("clip-path", "none");
  await expect(card).toHaveCSS("border-radius", "0px");
  await expect(page.locator(".hud-cnr")).toHaveCount(0);
  await expect(page.locator("#verify-gate")).toHaveClass(/\bnj-frame-quiet\b/);

  // The CTA opts into the ambient glitch, and it is a plate (the chamfer is paint, never geometry).
  await expect(page.locator("#connect")).toHaveClass(/\bshine\b/);
  await expect(page.locator("#connect")).toHaveClass(/\bnj-plate\b/);
  await expect(page.locator("#connect")).toHaveCSS("clip-path", "none");
});

// The <dialog> UA sheet sets `overflow: auto`, and the corner brackets sit 1px outside the box —
// so the panel grew scrollbars around its own frame (h + v), and `.modal-inner`'s
// `max-height: inherit` overflowed the dialog's border box by another 2px. Headless Chromium draws
// OVERLAY scrollbars, so a gutter measurement sees nothing; assert the conditions that *cause* a
// bar instead — a scroll container that overflows. docs/design.md §7: no horizontal overflow.
test("the how-it-works modal paints no scrollbars, and its body still scrolls", async ({
  page,
}) => {
  await page.setViewportSize({ width: 666, height: 1006 });
  await page.goto("/connect.html");
  await page.getByRole("button", { name: "How does it work?" }).click();

  const m = await page.evaluate(() => {
    const dlg = document.getElementById("how-modal")!;
    const inner = document.querySelector(".modal-inner") as HTMLElement;
    const dcs = getComputedStyle(dlg);
    const ics = getComputedStyle(inner);
    return {
      dlgOverflowX: dcs.overflowX,
      dlgOverflowY: dcs.overflowY,
      // The body must fit the panel's content box. `max-height: inherit` made it 2px taller
      // (the dialog's 1px borders), which is what overflowed the panel.
      innerFitsPanel: inner.offsetHeight <= dlg.clientHeight,
      innerOverflowsH: inner.scrollWidth > inner.clientWidth,
      innerScrollbarWidth: ics.scrollbarWidth,
      innerScrollable: inner.scrollHeight > inner.clientHeight,
    };
  });

  // The panel itself is never a scroll container; its body is.
  expect(m.dlgOverflowX).toBe("visible");
  expect(m.dlgOverflowY).toBe("visible");
  expect(m.innerFitsPanel, "modal body overflows the panel").toBe(true);
  // The scrolling body never overflows sideways, and paints no bar.
  expect(m.innerOverflowsH, "modal body overflows horizontally").toBe(false);
  expect(m.innerScrollbarWidth).toBe("none");

  // Hiding the bar must not strand the content: the body still scrolls to the wire-spec link.
  if (m.innerScrollable) {
    await page.locator(".modal-inner").hover();
    await page.mouse.wheel(0, 400);
    await expect
      .poll(() => page.locator(".modal-inner").evaluate((e) => e.scrollTop))
      .toBeGreaterThan(0);
  }
  await expect(
    page.getByRole("link", { name: /home-free-handshake\.md/ }),
  ).toBeVisible();
});

test("connect page explains the blind relay in a dismissible modal", async ({
  page,
}) => {
  await page.goto("/connect.html");

  const modal = page.locator("#how-modal");
  await expect(modal).toBeHidden();

  await page.getByRole("button", { name: "How does it work?" }).click();
  await expect(modal).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "The relay is blind" }),
  ).toBeVisible();
  // The trust claims that make the page worth believing.
  await expect(modal).toContainText("X25519");
  await expect(modal).toContainText("AES-256-GCM");
  await expect(modal).toContainText("Forward secrecy");
  await expect(
    modal.getByRole("link", { name: /home-free-handshake\.md/ }),
  ).toBeVisible();

  // Both new controls are thumb-sized on touch (design.md §8). Pixel 7 runs this at ≤800px.
  const isMobile = page.viewportSize()!.width <= 800;
  if (isMobile) {
    for (const name of ["How does it work?", "Close"]) {
      const box = await page.getByRole("button", { name }).boundingBox();
      expect(box!.height, `${name} touch target`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
  }

  await page.getByRole("button", { name: "Close" }).click();
  await expect(modal).toBeHidden();

  // Esc closes it too (native <dialog> focus trap).
  await page.getByRole("button", { name: "How does it work?" }).click();
  await expect(modal).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(modal).toBeHidden();
});

// docs/design.md §8: under reduced motion the atmosphere is a still frame and *zero* animation
// loops start — neither a rAF loop nor an endless CSS animation. Emulated per-page rather than via
// `test.use({ reducedMotion })`: the file-level `test.use` below wins over a describe-scoped one, and
// the fixture silently no-ops (matchMedia stayed false).
test("connect page starts no animation loop under reduced motion, and keeps its still atmosphere", async ({
  page,
}) => {
  await countRaf(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/connect.html");

  await expect(page.locator("html")).toHaveAttribute("data-motion", "reduced");
  await expect(page.locator(".nj-atmo .nj-hex")).toBeVisible();
  await expect(page.locator(".nj-atmo .nj-sweep")).toBeHidden();
  await page.waitForTimeout(600); // a loop, had one started, would have ticked by now
  expect(await rafCount(page)).toBe(0);
  expect((await atmoLoops(page)).filter((s) => s === "running")).toEqual([]);
});

// #1531: this shell owns the hidden-tab pause until the streamed app takes over: hiding the tab
// pauses the atmosphere's loops, showing it runs them again.
test("connect page pauses the atmosphere while hidden (#1531)", async ({ page }) => {
  // The atmosphere drifts on dark only (daylight is the still hex grid); headless defaults to light.
  await page.emulateMedia({ reducedMotion: "no-preference", colorScheme: "dark" });
  await page.goto("/connect.html");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "full");
  await expect.poll(() => atmoLoops(page)).toContain("running");

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.locator("html")).toHaveAttribute("data-motion-paused", "hidden");
  await expect.poll(() => atmoLoops(page)).not.toContain("running");

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.locator("html")).not.toHaveAttribute("data-motion-paused", /.+/);
  await expect.poll(() => atmoLoops(page)).toContain("running");
});

// #1601 review: once connected only the shell's OWN atmosphere gives way; the streamed app's
// <Atmosphere /> inside #app-root is the app's and stays visible.
test("connected: the shell's atmosphere goes, the streamed app's own atmosphere stays (#1596)", async ({ page, baseURL }) => {
  await page.emulateMedia({ reducedMotion: "no-preference", colorScheme: "dark" });
  await stubSuccessfulConnect(page, { appAtmosphere: true });
  await page.goto(publicConnectUrl(baseURL));
  await page.getByLabel("Console key").fill("nightjar-1010");
  await page.getByLabel("Access password").fill("stream-secret");
  await verifyHuman(page);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.locator(".session-box")).toBeVisible();

  await expect(page.locator("body > .nj-atmo")).toBeHidden();
  const appAtmo = page.getByTestId("app-atmo");
  await expect(appAtmo).toBeVisible();
  await expect(appAtmo.locator(".nj-hex")).toBeVisible();
  await expect(appAtmo.locator(".nj-sweep")).toBeVisible();
});

// #1596: no setting of its own, so the page follows the device's colour scheme — unless this
// browser holds a BattleLab theme choice (the operator on their own device).
test("connect page follows the device's colour scheme, and a stored choice wins", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/connect.html");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  // Daylight: the hex grid only.
  await expect(page.locator(".nj-atmo .nj-hex")).toBeVisible();
  await expect(page.locator(".nj-atmo .nj-vig")).toBeHidden();

  await page.evaluate(() => localStorage.setItem("tr-theme", "dark"));
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

// docs/design.md v2 §8: the ambient layer idles under 2 % main-thread time in Full.
test("connect page idles cheaply in Full (the shared idle budget)", async ({ page, browserName }, info) => {
  test.skip(browserName !== "chromium", "CDP metrics");
  test.skip(info.project.name !== "desktop", "one measurement is enough");
  // The atmosphere drifts on dark only (daylight is the still hex grid); headless defaults to light.
  await page.emulateMedia({ reducedMotion: "no-preference", colorScheme: "dark" });
  await page.goto("/connect.html");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "full");
  await expectIdleBudget(page, "connect", info);
});

test.use({
  launchOptions: {
    args: ["--host-resolver-rules=MAP battlelab.superstatus.io 127.0.0.1"],
    // Replaces the config's launchOptions, so repeat its shared-memory choice (playwright.config.ts).
    ignoreDefaultArgs: ["--disable-dev-shm-usage"],
  },
});

const PUBLIC_RELAY = "https://relay.battlelab.superstatus.io";
const STORAGE_KEY = "battlelab.connect.session.v1";
const ZERO_ALTCHA =
  "5feceb66ffc86f38d952786c6d696c79c2dbc239dd4e91b46729d73a27fb57e9";

function publicConnectUrl(baseURL: string | undefined): string {
  const u = new URL(baseURL ?? "http://localhost:41873");
  return `http://battlelab.superstatus.io:${u.port}/connect.html?relay=https://evil.example&token=leak`;
}

async function stubSuccessfulConnect(
  page: import("@playwright/test").Page,
  opts: { appAtmosphere?: boolean } = {},
): Promise<void> {
  await page.addInitScript(({ appAtmosphere }) => {
    window.__battlelabConnectHarness = {
      holdMs: 250, // keep the human-gate hold short in tests
      makeWebSocket: () => ({
        binaryType: "arraybuffer",
        onopen: null,
        onmessage: null,
        onclose: null,
        onerror: null,
        readyState: 1,
        send() {},
        close() {},
      }),
      mountApp: async (_ws, _key, _captcha, opts) => {
        opts?.onEvent?.({
          type: "paired",
          deadline: Math.floor(Date.now() / 1000) + 14400,
          ttl: 14400,
        });
        const root = document.getElementById("app-root");
        if (root) {
          root.textContent = "Mock streamed BattleLab app";
          // The streamed SPA mounts its own <Atmosphere /> (App.tsx) inside #app-root.
          if (appAtmosphere)
            root.insertAdjacentHTML(
              "beforeend",
              '<div class="nj-atmo" aria-hidden="true" data-testid="app-atmo"><i class="nj-hex"></i><i class="nj-scan"></i><i class="nj-noise"></i><i class="nj-sweep"></i><i class="nj-vig"></i></div>',
            );
        }
        return {
          teardown: () => {
            if (root) root.textContent = "";
          },
        };
      },
    };
  }, { appAtmosphere: opts.appAtmosphere ?? false });
  await page.route(
    "https://relay.battlelab.superstatus.io/altcha/challenge",
    async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          algorithm: "SHA-256",
          challenge: ZERO_ALTCHA,
          salt: "",
          signature: "test",
          maxnumber: 0,
        }),
      });
    },
  );
}

test("connect sign-in is centered and keeps custom relay in advanced controls", async ({
  page,
}) => {
  await page.goto("/connect.html");

  const card = page.locator(".connect-card");
  await expect(card).toBeVisible();
  await expect(page.getByLabel("Console key")).toBeVisible();
  await expect(page.getByLabel("Access password")).toBeVisible();
  await expect(page.getByRole("button", { name: "Connect" })).toBeVisible();

  const box = await card.boundingBox();
  const viewport = page.viewportSize();
  expect(box).not.toBeNull();
  expect(viewport).not.toBeNull();
  expect(Math.abs(box!.x + box!.width / 2 - viewport!.width / 2)).toBeLessThan(
    80,
  );

  await page.getByText("Custom relay").click();
  await expect(page.getByLabel("Relay base URL")).toBeVisible();
});

test("public connect signs in, canonicalizes the URL, stores credentials for the relay-announced 4-hour window, and signs out", async ({
  page,
  baseURL,
}) => {
  await stubSuccessfulConnect(page);
  await page.goto(publicConnectUrl(baseURL));

  await expect(page.getByLabel("Relay base URL")).toBeHidden();
  await page.getByLabel("Console key").fill("viper-8231");
  await page.getByLabel("Access password").fill("stream-secret");
  await verifyHuman(page);
  await page.getByRole("button", { name: "Connect" }).click();

  await expect(page.locator(".session-box")).toBeVisible();
  await expect(page.locator(".connect-card")).toBeHidden();
  await expect(page).toHaveURL(/\/connect\/$/);
  const saved = await page.evaluate(
    (key) => sessionStorage.getItem(key),
    STORAGE_KEY,
  );
  expect(saved).toBeTruthy();
  const parsed = JSON.parse(saved!);
  expect(parsed).toMatchObject({
    relay: PUBLIC_RELAY,
    name: "viper-8231",
    key: "stream-secret",
  });
  // The relay announced a 4-hour deadline — the saved window must follow it and
  // must NOT be clamped back to the old one-hour cap (#662 regression).
  expect(parsed.expiresAt).toBeGreaterThan(Date.now() + 3_600_000);
  expect(parsed.expiresAt).toBeLessThanOrEqual(Date.now() + 14_400_000);

  // #684: on narrow viewports the bar defaults collapsed, hiding Sign out — expand it first.
  if (
    ((await page.locator(".session-box").getAttribute("class")) ?? "").includes(
      "collapsed",
    )
  ) {
    await page.locator("#session-toggle").click();
  }
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.locator(".connect-card")).toBeVisible();
  await expect(
    page.evaluate((key) => sessionStorage.getItem(key), STORAGE_KEY),
  ).resolves.toBeNull();
  await expect(page.getByLabel("Access password")).toHaveValue("");
});

test("expired saved credentials are ignored", async ({ page, baseURL }) => {
  await page.addInitScript(
    ({ key, relay }) => {
      sessionStorage.setItem(
        key,
        JSON.stringify({
          relay,
          name: "old-box",
          key: "old-secret",
          expiresAt: Date.now() - 1000,
        }),
      );
    },
    { key: STORAGE_KEY, relay: PUBLIC_RELAY },
  );

  await page.goto(publicConnectUrl(baseURL));

  await expect(page.locator(".connect-card")).toBeVisible();
  await expect(page.getByLabel("Console key")).toHaveValue("");
  await expect(
    page.evaluate((key) => sessionStorage.getItem(key), STORAGE_KEY),
  ).resolves.toBeNull();
});

async function streamConnected(
  page: import("@playwright/test").Page,
  baseURL: string | undefined,
): Promise<void> {
  await stubSuccessfulConnect(page);
  await page.goto(publicConnectUrl(baseURL));
  await page.getByLabel("Console key").fill("nightjar-1010");
  await page.getByLabel("Access password").fill("stream-secret");
  await verifyHuman(page);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.locator(".session-box")).toBeVisible();
}

// #684: the connection bar collapses to a small status toggle so it doesn't block the app
// toolbar on small screens. Default follows the viewport; a manual choice wins and persists.
test("connection bar: collapses per viewport, the toggle flips it, expanded stays upper-center", async ({
  page,
  baseURL,
}) => {
  await streamConnected(page, baseURL);
  const box = page.locator(".session-box");
  const toggle = page.locator("#session-toggle");
  const signout = page.getByRole("button", { name: /sign out/i });
  const narrow = page.viewportSize()!.width <= 800;

  // Default state follows the viewport when the user hasn't chosen.
  if (narrow) {
    await expect(box).toHaveClass(/collapsed/);
    await expect(signout).toBeHidden();
  } else {
    await expect(box).not.toHaveClass(/collapsed/);
    await expect(signout).toBeVisible();
    const b = (await box.boundingBox())!;
    const vw = page.viewportSize()!.width;
    expect(b.y).toBeLessThan(28); // still floats at the upper center when expanded
    expect(Math.abs(b.x + b.width / 2 - vw / 2)).toBeLessThan(32);
  }

  // The toggle is a real ≥44px touch target with a correct expanded state + accessible name.
  const tb = (await toggle.boundingBox())!;
  expect(tb.width).toBeGreaterThanOrEqual(40);
  expect(tb.height).toBeGreaterThanOrEqual(40);
  await expect(toggle).toHaveAttribute("aria-expanded", String(!narrow));
  await expect(toggle).toHaveAttribute("aria-label", /connection bar/i);

  // Toggling flips collapsed state and sign-out reachability.
  await toggle.click();
  if (narrow) {
    await expect(box).not.toHaveClass(/collapsed/);
    await expect(signout).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
  } else {
    await expect(box).toHaveClass(/collapsed/);
    await expect(signout).toBeHidden();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
  }
});

test("collapsed connection bar is click-through beside the toggle and persists the choice", async ({
  page,
  baseURL,
}) => {
  await streamConnected(page, baseURL);
  const box = page.locator(".session-box");
  const toggle = page.locator("#session-toggle");

  // Deterministically make an explicit "collapsed" choice from either viewport default.
  if (((await box.getAttribute("class")) ?? "").includes("collapsed"))
    await toggle.click();
  await expect(box).not.toHaveClass(/collapsed/);
  await toggle.click();
  await expect(box).toHaveClass(/collapsed/);

  // The collapsed footprint is a small chip, not the full-width bar…
  const bb = (await box.boundingBox())!;
  expect(bb.width).toBeLessThan(140);
  // …and a point just beside it resolves to the app underneath, not the bar (chrome is
  // pointer-events:none so toolbar controls stay clickable).
  const hit = await page.evaluate(
    ({ x, y }) =>
      document.elementFromPoint(x, y)?.closest(".session-box")
        ? "bar"
        : "through",
    { x: bb.x + bb.width + 40, y: bb.y + bb.height / 2 },
  );
  expect(hit).toBe("through");

  // The explicit choice is persisted in its own key, so a later reconnect restores it via
  // applyBarDefault() (which reads the override before falling back to the viewport default).
  expect(
    await page.evaluate(() =>
      sessionStorage.getItem("battlelab.connect.bar.v1"),
    ),
  ).toBe("collapsed");
});

test("connection bar default tracks viewport resize while no explicit choice is set", async ({
  page,
  baseURL,
}) => {
  await streamConnected(page, baseURL);
  const box = page.locator(".session-box");

  // No explicit choice yet → the default follows the viewport across the 800px breakpoint
  // (desktop→mobile resize/rotate must auto-collapse, per #684's contract).
  await page.setViewportSize({ width: 1280, height: 800 });
  await expect(box).not.toHaveClass(/collapsed/);
  await page.setViewportSize({ width: 700, height: 900 });
  await expect(box).toHaveClass(/collapsed/);
  await page.setViewportSize({ width: 1280, height: 800 });
  await expect(box).not.toHaveClass(/collapsed/);

  // An explicit choice wins over later viewport changes.
  await page.locator("#session-toggle").click(); // explicitly collapse at desktop width
  await expect(box).toHaveClass(/collapsed/);
  await page.setViewportSize({ width: 1400, height: 900 }); // widen — the choice must stick
  await expect(box).toHaveClass(/collapsed/);
});

test("a manual collapse choice survives a resize even when sessionStorage is blocked", async ({
  page,
  baseURL,
}) => {
  // #684: with the bar key's setItem throwing (storage blocked), the choice is only in memory —
  // the resize listener must still treat it as an override and not revert to the viewport default.
  await page.addInitScript(() => {
    const orig = Storage.prototype.setItem;
    Storage.prototype.setItem = function (k, v) {
      if (k === "battlelab.connect.bar.v1")
        throw new DOMException("blocked", "SecurityError");
      return orig.call(this, k, v);
    };
  });
  await streamConnected(page, baseURL);
  const box = page.locator(".session-box");
  const toggle = page.locator("#session-toggle");

  // Make an explicit collapse choice at desktop width (nothing gets persisted — storage throws).
  // The setup must WAIT for the widening to settle before touching the toggle (#734). Reading the
  // class and then conditionally clicking raced the `(max-width: 800px)` change handler: on the
  // mobile project the bar starts collapsed, so a read that lands before the handler decides to
  // click, the handler then expands, and the click arrives on an already-expanded bar and collapses
  // it — `expect(box).not.toHaveClass(/collapsed/)` then sees exactly "session-box collapsed".
  // A web-first assertion on the no-override viewport default is the settled state to start from,
  // so the single click below is always the FIRST explicit choice, on every project.
  await page.setViewportSize({ width: 1280, height: 800 });
  await expect(box).not.toHaveClass(/collapsed/); // wide + no override ⇒ expanded, once settled
  await toggle.click(); // …and this is unambiguously the explicit collapse choice
  await expect(box).toHaveClass(/collapsed/);

  // A breakpoint round-trip must NOT revert the in-memory choice.
  await page.setViewportSize({ width: 700, height: 900 });
  await page.setViewportSize({ width: 1280, height: 800 });
  await expect(box).toHaveClass(/collapsed/);
});

test("a newer in-memory choice beats a stale readable stored value on reconnect", async ({
  page,
  baseURL,
}) => {
  // #684: storage holds an old value ('collapsed') but writes are blocked; the user then makes the
  // opposite explicit choice (expand) in-memory. On reconnect, applyBarDefault must honour the
  // in-memory choice, not overwrite it with the stale stored value.
  await page.addInitScript(() => {
    const orig = Storage.prototype.setItem;
    let seeded = false;
    Storage.prototype.setItem = function (k, v) {
      if (k === "battlelab.connect.bar.v1") {
        if (!seeded) {
          seeded = true;
          return orig.call(this, k, v); // allow the one seed write, block the rest
        }
        throw new DOMException("blocked", "SecurityError");
      }
      return orig.call(this, k, v);
    };
    sessionStorage.setItem("battlelab.connect.bar.v1", "collapsed"); // stale stored value
  });
  await streamConnected(page, baseURL);
  const box = page.locator(".session-box");
  const toggle = page.locator("#session-toggle");

  // The stored 'collapsed' applies on connect; explicitly EXPAND (the write throws → memory only).
  await expect(box).toHaveClass(/collapsed/);
  await toggle.click();
  await expect(box).not.toHaveClass(/collapsed/);
  expect(
    await page.evaluate(() =>
      sessionStorage.getItem("battlelab.connect.bar.v1"),
    ),
  ).toBe(
    "collapsed", // storage stayed stale — the write was blocked
  );

  // Sign out (reachable now it's expanded) and reconnect in the same page view.
  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page.locator(".connect-card")).toBeVisible();
  await page.getByLabel("Console key").fill("nightjar-1010");
  await page.getByLabel("Access password").fill("stream-secret");
  await verifyHuman(page); // sign-out re-gated the next attempt
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(box).toBeVisible();
  // The newer in-memory 'expanded' must win over the stale stored 'collapsed'.
  await expect(box).not.toHaveClass(/collapsed/);
});

/** The relay's side of a connect, through the REAL mount path (#1596): the fake socket answers the
 *  `hello` with a relay error frame, or never answers (the attempt stays in flight). */
async function stubRelay(page: import("@playwright/test").Page, outcome: "offline" | "hang" | "paired"): Promise<void> {
  await page.addInitScript((outcome) => {
    window.__battlelabConnectHarness = {
      holdMs: 250,
      makeWebSocket: () => {
        const ws = {
          binaryType: "arraybuffer",
          readyState: 1,
          onopen: null,
          onclose: null,
          onerror: null,
          onmessage: null as null | ((e: { data: string }) => void),
          send(d: unknown) {
            if (outcome === "offline" && typeof d === "string" && d.includes('"hello"'))
              setTimeout(() => ws.onmessage?.({ data: JSON.stringify({ t: "error", code: "offline" }) }), 0);
            // The relay pairs the box, then the box never answers the key exchange.
            if (outcome === "paired" && typeof d === "string" && d.includes('"hello"'))
              setTimeout(
                () =>
                  ws.onmessage?.({
                    data: JSON.stringify({ t: "paired", deadline: Math.floor(Date.now() / 1000) + 14400, ttl: 14400 }),
                  }),
                0,
              );
          },
          close() {},
        };
        return ws;
      },
    };
  }, outcome);
  await page.route("https://relay.battlelab.superstatus.io/altcha/challenge", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ algorithm: "SHA-256", challenge: ZERO_ALTCHA, salt: "", signature: "test", maxnumber: 0 }),
    }),
  );
}

async function attempt(page: import("@playwright/test").Page): Promise<void> {
  await verifyHuman(page);
  await page.getByRole("button", { name: "Connect" }).click();
}

test("connecting puts the form away for the radar, the steps and the acquire bar (#1596)", async ({ page, baseURL }) => {
  await stubRelay(page, "hang");
  await page.goto(publicConnectUrl(baseURL));
  await page.getByLabel("Console key").fill("viper-8231");
  await page.getByLabel("Access password").fill("stream-secret");
  await attempt(page);

  const progress = page.locator("#connect-progress");
  await expect(progress).toBeVisible();
  await expect(page.locator("#connect-form")).toBeHidden();
  await expect(progress.locator(".nj-radar")).toBeVisible();
  await expect(progress.locator(".nj-acquire")).toBeVisible();
  await expect(progress).toContainText("viper-8231");
  // The challenge is solved and the relay socket is open: the box is next (#1601 review: the card
  // used to stay at "Relay reached" once the socket opened).
  await expect(progress.locator(".cs-step-done .cs-step-label")).toHaveText(["Relay challenge", "Relay reached"]);
  await expect(progress.locator(".cs-step-active")).toContainText("Box found");
  await expect(page.locator("#uplink-label")).toHaveText("Uplink // Acquiring");
});

// #1601 review: every transition renders — the socket opens (box next), the relay pairs the box
// (key exchange next), and only a finished key exchange hands the screen to the streamed app.
// Through the REAL mount path: the relay pairs, the box never answers the key exchange.
test("a paired box shows the key exchange in flight; the card stays until it finishes (#1596)", async ({ page }) => {
  await stubRelay(page, "paired");
  // The local page, not the public host: the key exchange needs WebCrypto, which only a secure
  // context (127.0.0.1 is one; http://battlelab.superstatus.io is not) has.
  await page.goto("/connect.html");
  await page.getByText("Custom relay").click();
  await page.getByLabel("Relay base URL").fill(PUBLIC_RELAY);
  await page.getByLabel("Console key").fill("viper-8231");
  await page.getByLabel("Access password").fill("stream-secret");
  await attempt(page);

  const progress = page.locator("#connect-progress");
  await expect(progress.locator(".cs-step-active")).toContainText("Key exchange");
  await expect(progress.locator(".cs-step-done")).toHaveCount(3);
  await expect(page.locator("#status")).toContainText("exchanging keys");
  // Not live yet: the card holds the screen, no session bar, nothing saved.
  await expect(page.locator(".connect-card")).toBeVisible();
  await expect(page.locator(".session-box")).toBeHidden();
  await expect(page.locator("body")).toHaveAttribute("data-state", "connecting");
  expect(await page.evaluate((key) => sessionStorage.getItem(key), STORAGE_KEY)).toBeNull();
});

// #1601 review 6800: a finished key exchange no longer hands over the screen — the card stays (all
// four steps done, the app loading) until the mount resolves. Contract change: this spec pinned
// "live before the mount even resolves", which left an empty app and a Sign out racing the mount.
test("the connect card walks every step: relay reached, box found, key exchange, then the app (#1596)", async ({ page, baseURL }) => {
  // A mount that reports each step and waits for the test to release the next one.
  await page.addInitScript(() => {
    const w = window as unknown as { __next?: () => void };
    const step = () => new Promise<void>((r) => (w.__next = r));
    window.__battlelabConnectHarness = {
      holdMs: 250,
      makeWebSocket: () => ({ binaryType: "arraybuffer", onopen: null, onmessage: null, onclose: null, onerror: null, readyState: 1, send() {}, close() {} }),
      mountApp: async (_ws, _key, _captcha, opts) => {
        opts?.onEvent?.({ type: "open" });
        await step();
        opts?.onEvent?.({ type: "paired", deadline: Math.floor(Date.now() / 1000) + 14400, ttl: 14400 });
        await step();
        opts?.onEvent?.({ type: "secured" });
        await step();
        const root = document.getElementById("app-root");
        if (root) root.textContent = "Mock streamed BattleLab app";
        return { teardown: () => {} };
      },
    };
  });
  await page.route("https://relay.battlelab.superstatus.io/altcha/challenge", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ algorithm: "SHA-256", challenge: ZERO_ALTCHA, salt: "", signature: "test", maxnumber: 0 }),
    }),
  );
  await page.goto(publicConnectUrl(baseURL));
  await page.getByLabel("Console key").fill("viper-8231");
  await page.getByLabel("Access password").fill("stream-secret");
  await attempt(page);
  const next = () => page.evaluate(() => (window as unknown as { __next: () => void }).__next());

  const progress = page.locator("#connect-progress");
  const active = progress.locator(".cs-step-active");
  await expect(active).toContainText("Box found");
  await expect(progress.locator(".cs-step-done")).toHaveCount(2);
  await expect(page.locator("#status")).toContainText("finding your box");

  await next();
  await expect(active).toContainText("Key exchange");
  await expect(progress.locator(".cs-step-done")).toHaveCount(3);
  await expect(page.locator(".session-box")).toBeHidden();

  await next(); // secured: every step done, the app still mounting — the card holds the screen
  await expect(progress.locator(".cs-step-done")).toHaveCount(4);
  await expect(page.locator("#status")).toContainText("loading your BattleLab");
  await expect(page.locator(".connect-card")).toBeVisible();
  await expect(page.locator(".session-box")).toBeHidden();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeHidden();
  await expect(page.locator("body")).toHaveAttribute("data-state", "connecting");
  expect(await page.evaluate((key) => sessionStorage.getItem(key), STORAGE_KEY)).toBeNull();

  await next(); // the mount resolves: now, and only now, the app takes the screen
  await expect(page.locator(".session-box")).toBeVisible();
  await expect(page.locator(".connect-card")).toBeHidden();
  await expect(page.locator("body")).toHaveAttribute("data-state", "connected");
  await expect(page.locator("#app-root")).toHaveText("Mock streamed BattleLab app");
  expect(await page.evaluate((key) => sessionStorage.getItem(key), STORAGE_KEY)).toBeTruthy();
});

/** A deferred mount (#1601 review 6800): each attempt reports open → paired → secured, then waits
 *  for the test to release it. `__mounts[i]` records its fake relay socket (closed?), how often its
 *  app was torn down, and lets the test push relay events into it. */
interface DeferredMount {
  release: () => void;
  emit: (evt: { type: "closed" } | { type: "expired" }) => void;
  wsClosed: boolean;
  teardowns: number;
}
async function stubDeferredMount(page: import("@playwright/test").Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __mounts: DeferredMount[] };
    w.__mounts = [];
    const sockets: { closed: boolean }[] = [];
    window.__battlelabConnectHarness = {
      holdMs: 250,
      makeWebSocket: () => {
        const ws = {
          closed: false,
          binaryType: "arraybuffer",
          onopen: null,
          onmessage: null,
          onclose: null,
          onerror: null,
          readyState: 1,
          send() {},
          close() {
            ws.closed = true;
          },
        };
        sockets.push(ws);
        return ws;
      },
      mountApp: async (_ws, _key, _captcha, opts) => {
        const ws = sockets[sockets.length - 1];
        const n = w.__mounts.length;
        let release!: () => void;
        const released = new Promise<void>((r) => (release = r));
        const rec: DeferredMount = {
          release,
          emit: (evt) => opts?.onEvent?.(evt),
          get wsClosed() {
            return ws.closed;
          },
          teardowns: 0,
        };
        w.__mounts.push(rec);
        opts?.onEvent?.({ type: "open" });
        opts?.onEvent?.({ type: "paired", deadline: Math.floor(Date.now() / 1000) + 14400, ttl: 14400 });
        opts?.onEvent?.({ type: "secured" });
        await released;
        // What the real mount does once the bundle lands: render into #app-root over a live tunnel.
        const root = document.getElementById("app-root")!;
        const app = document.createElement("div");
        app.dataset.testid = `late-app-${n}`;
        app.textContent = `streamed app ${n}`;
        root.append(app);
        return {
          teardown: () => {
            rec.teardowns += 1;
            app.remove();
            ws.close(); // wired.teardown → session.close → the relay socket
          },
        };
      },
    };
  });
  await page.route("https://relay.battlelab.superstatus.io/altcha/challenge", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ algorithm: "SHA-256", challenge: ZERO_ALTCHA, salt: "", signature: "test", maxnumber: 0 }),
    }),
  );
}

const mounts = (page: import("@playwright/test").Page) =>
  page.evaluate(() =>
    (window as unknown as { __mounts: DeferredMount[] }).__mounts.map((m) => ({
      wsClosed: m.wsClosed,
      teardowns: m.teardowns,
    })),
  );
const releaseMount = (page: import("@playwright/test").Page, i: number) =>
  page.evaluate((i) => (window as unknown as { __mounts: DeferredMount[] }).__mounts[i].release(), i);

async function startDeferred(page: import("@playwright/test").Page, baseURL: string | undefined): Promise<void> {
  await stubDeferredMount(page);
  await page.goto(publicConnectUrl(baseURL));
  await page.getByLabel("Console key").fill("viper-8231");
  await page.getByLabel("Access password").fill("stream-secret");
  await attempt(page);
  await expect.poll(async () => (await mounts(page)).length).toBe(1);
  // The key exchange finished (the mount reported it synchronously, before awaiting the release);
  // the app is still mounting: the card holds the screen. Read once and soft, so a regression also
  // reports what the late mount then does (the leak), not only this.
  expect.soft(await page.locator("body").getAttribute("data-state")).toBe("connecting");
  expect.soft(await page.locator(".connect-card").isVisible()).toBe(true);
  expect.soft(await page.locator(".session-box").isVisible()).toBe(false);
  expect.soft(await page.locator("#connect-progress .cs-step-done").count()).toBe(4);
}

/** A late mount of an ended attempt: torn down once, nothing in #app-root, its socket closed, and
 *  the page still signed out with nothing saved. */
async function expectLateMountTornDown(page: import("@playwright/test").Page, i = 0): Promise<void> {
  await releaseMount(page, i);
  await expect.poll(async () => (await mounts(page))[i].teardowns).toBe(1);
  await expect(page.locator(`[data-testid="late-app-${i}"]`)).toHaveCount(0);
  expect((await mounts(page))[i].wsClosed).toBe(true);
  await expect(page.locator("body")).toHaveAttribute("data-state", "signed-out");
  await expect(page.locator(".connect-card")).toBeVisible();
  await expect(page.locator(".session-box")).toBeHidden();
  expect(await page.evaluate((key) => sessionStorage.getItem(key), STORAGE_KEY)).toBeNull();
}

test("signing out while the app still mounts tears the late app and its tunnel down (#1601 review 6800)", async ({ page, baseURL }) => {
  await startDeferred(page, baseURL);
  // No Sign out to reach while the card holds the screen…
  expect.soft(await page.getByRole("button", { name: "Sign out" }).isVisible()).toBe(false);
  // …and should one fire anyway (the review's repro), it ends the attempt: the relay socket closes
  // at once, before the mount has even landed.
  await page.evaluate(() => document.getElementById("signout")!.click());
  await expect(page.locator("#status")).toContainText("signed out");
  await expect.configure({ soft: true }).poll(async () => (await mounts(page))[0].wsClosed).toBe(true);
  await expectLateMountTornDown(page);
});

for (const evt of ["closed", "expired"] as const) {
  test(`a relay ${evt} while the app still mounts tears the late app down (#1601 review 6800)`, async ({ page, baseURL }) => {
    await startDeferred(page, baseURL);
    await page.evaluate((type) => (window as unknown as { __mounts: DeferredMount[] }).__mounts[0].emit({ type }), evt);
    await expect(page.locator("#status")).toContainText(evt === "closed" ? "disconnected" : "expired");
    await expect.configure({ soft: true }).poll(async () => (await mounts(page))[0].wsClosed).toBe(true);
    await expectLateMountTornDown(page);
    // The torn-down app's own late close is not news: the page keeps the relay's message.
    await expect(page.locator("#status")).toContainText(evt === "closed" ? "disconnected" : "expired");
  });
}

test("a second connect waits for the ended attempt's late mount, which never touches it (#1601 review 6800)", async ({ page, baseURL }) => {
  await startDeferred(page, baseURL);
  await page.evaluate(() => (window as unknown as { __mounts: DeferredMount[] }).__mounts[0].emit({ type: "closed" }));
  await expect(page.locator("body")).toHaveAttribute("data-state", "signed-out");

  await attempt(page); // attempt B, while A's mount is still out
  await expect(page.locator("body")).toHaveAttribute("data-state", "connecting");
  expect(await mounts(page)).toHaveLength(1); // B does not mount over A's in-flight app

  await releaseMount(page, 0); // A lands late: torn down, and B goes on
  await expect.poll(async () => (await mounts(page))[0].teardowns).toBe(1);
  await expect(page.locator('[data-testid="late-app-0"]')).toHaveCount(0);
  await expect.poll(async () => (await mounts(page)).length).toBe(2);
  await expect(page.locator(".connect-card")).toBeVisible();

  await releaseMount(page, 1);
  await expect(page.locator("body")).toHaveAttribute("data-state", "connected");
  await expect(page.locator('[data-testid="late-app-1"]')).toBeVisible();
  const [a, b] = await mounts(page);
  expect(a).toEqual({ wsClosed: true, teardowns: 1 });
  expect(b).toEqual({ wsClosed: false, teardowns: 0 });
  expect(await page.evaluate((key) => sessionStorage.getItem(key), STORAGE_KEY)).toBeTruthy();

  // Signing B out tears it down exactly once.
  if (((await page.locator(".session-box").getAttribute("class")) ?? "").includes("collapsed"))
    await page.locator("#session-toggle").click();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect.poll(async () => (await mounts(page))[1]).toEqual({ wsClosed: true, teardowns: 1 });
  await expect(page.locator('[data-testid="late-app-1"]')).toHaveCount(0);
});

test("a relay failure shows the error card: Back returns to the key, Retry to the gate (#1596)", async ({ page, baseURL }) => {
  await stubRelay(page, "offline");
  await page.goto(publicConnectUrl(baseURL));
  await page.getByLabel("Console key").fill("viper-8231");
  await page.getByLabel("Access password").fill("stream-secret");
  await attempt(page);

  const card = page.locator("#connect-error");
  await expect(card).toBeVisible();
  await expect(card).toHaveAttribute("role", "alert");
  await expect(card.getByRole("heading", { name: "Your box is offline" })).toBeVisible();
  await expect(card.locator(".cs-step-failed")).toContainText("Box found");
  await expect(card.locator(".cs-step-done")).toHaveCount(2);
  await expect(page.locator(".connect-card")).toHaveAttribute("data-view", "error");
  await expect(page.locator("#uplink-label")).toHaveText("Uplink // Failed");
  await expect(page.locator("#connect-form")).toBeHidden();
  // The status line keeps the machine-readable code for anyone reading it later.
  await expect(page.locator("#status")).toContainText("could not connect: offline");
  for (const name of ["Back", "Retry"]) {
    const box = (await card.getByRole("button", { name }).boundingBox())!;
    expect(box.height, `${name} touch target`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await expect(card.getByRole("button", { name: "Retry" })).toBeFocused();

  // Back: the form again, at the console key, with what was typed still there.
  await card.getByRole("button", { name: "Back" }).click();
  await expect(card).toBeHidden();
  await expect(page.getByLabel("Console key")).toBeFocused();
  await expect(page.getByLabel("Console key")).toHaveValue("viper-8231");
  await expect(page.locator(".connect-card")).toHaveAttribute("data-view", "form");

  // Retry: the form again, at the gate — the next attempt needs a fresh hold (#690).
  await attempt(page);
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#gate-hold")).toBeFocused();
  await expect(page.locator("#verify-gate")).toHaveAttribute("data-state", "idle");
  await expect(page.getByRole("button", { name: "Connect" })).toBeDisabled();
});

test("a card taller than the screen scrolls to its Connect button", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 560 });
  await page.goto("/connect.html");
  const connect = page.getByRole("button", { name: "Connect" });
  await connect.scrollIntoViewIfNeeded();
  await expect(connect).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
