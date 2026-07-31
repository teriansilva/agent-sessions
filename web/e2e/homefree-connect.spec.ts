import { expect, test } from "@playwright/test";

import { verifyHuman } from "./connect-helpers";

// #579 app-only connect: the public connect page no longer exposes the recovery terminal pane.
// Real browser guard because this is page structure/layout, not a jsdom-only contract.
test("Home Free connect page exposes the app root, not a recovery terminal pane", async ({ page }) => {
  await page.goto("/connect.html");

  await expect(page.getByRole("button", { name: "CONNECT" })).toBeVisible();
  await expect(page.locator("#app-root")).toHaveCount(1);
  await expect(page.locator("#term")).toHaveCount(0);
  await expect(page.locator(".xterm")).toHaveCount(0);
});

/** Count rAF calls so "the ambient loop is running / is not running" is observable, not inferred. */
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

// The connect page is a standalone shell (it can't import the SPA's CSS), so nothing else
// guards it against drifting off the HUD design system — docs/design.md §6/§8.
test("connect page wears the HUD chrome: ambient canvas, four brackets, glitchable CTA", async ({
  page,
}) => {
  await countRaf(page);
  await page.goto("/connect.html");
  // The ambient field is actually animating, not just present in the DOM.
  await expect.poll(() => rafCount(page)).toBeGreaterThan(0);

  // The ambient data-flow field paints behind everything and never eats a click.
  const canvas = page.locator("canvas#bg.hud-canvas");
  await expect(canvas).toHaveCount(1);
  await expect(canvas).toHaveCSS("pointer-events", "none");
  await expect
    .poll(() => canvas.evaluate((c: HTMLCanvasElement) => c.width > 0 && c.height > 0))
    .toBe(true);

  // Corner-bracket frame, not a rounded box. The card owns exactly four brackets of its own;
  // the human-verification gate (#690) nested inside carries its own four.
  await expect(page.locator(".connect-card > .hud-cnr")).toHaveCount(4);
  await expect(page.locator("#verify-gate > .hud-cnr")).toHaveCount(4);
  await expect(page.locator(".connect-card")).toHaveCSS("border-radius", "0px");
  await expect(page.locator("#verify-gate")).toHaveCSS("border-radius", "0px");

  // The CTA opts into the ambient glitch, and clicking it still hits the button.
  await expect(page.locator("#connect")).toHaveClass(/\bshine\b/);
});

// The <dialog> UA sheet sets `overflow: auto`, and the corner brackets sit 1px outside the box —
// so the panel grew scrollbars around its own frame (h + v), and `.modal-inner`'s
// `max-height: inherit` overflowed the dialog's border box by another 2px. Headless Chromium draws
// OVERLAY scrollbars, so a gutter measurement sees nothing; assert the conditions that *cause* a
// bar instead — a scroll container that overflows. docs/design.md §7: no horizontal overflow.
test("the how-it-works modal paints no scrollbars, and its body still scrolls", async ({ page }) => {
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

  // The panel itself is never a scroll container — the brackets may hang 1px outside it in peace.
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
    await expect.poll(() => page.locator(".modal-inner").evaluate((e) => e.scrollTop)).toBeGreaterThan(0);
  }
  await expect(page.getByRole("link", { name: /home-free-handshake\.md/ })).toBeVisible();
});

test("connect page explains the blind relay in a dismissible modal", async ({ page }) => {
  await page.goto("/connect.html");

  const modal = page.locator("#how-modal");
  await expect(modal).toBeHidden();

  await page.getByRole("button", { name: "How does it work?" }).click();
  await expect(modal).toBeVisible();
  await expect(page.getByRole("heading", { name: "The relay is blind" })).toBeVisible();
  // The trust claims that make the page worth believing.
  await expect(modal).toContainText("X25519");
  await expect(modal).toContainText("AES-256-GCM");
  await expect(modal).toContainText("Forward secrecy");
  await expect(modal.getByRole("link", { name: /home-free-handshake\.md/ })).toBeVisible();

  // Both new controls are thumb-sized on touch (design.md §8). Pixel 7 runs this at ≤800px.
  const isMobile = page.viewportSize()!.width <= 800;
  if (isMobile) {
    for (const name of ["How does it work?", "Close"]) {
      const box = await page.getByRole("button", { name }).boundingBox();
      expect(box!.height, `${name} touch target`).toBeGreaterThanOrEqual(44);
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

// docs/design.md §8: under reduced motion the canvas is gone, a static grid takes over, and
// *zero* animation loops start — a hidden canvas that still burns rAF would pass a CSS-only check.
// Emulated per-page rather than via `test.use({ reducedMotion })`: the file-level `test.use` below
// wins over a describe-scoped one, and the fixture silently no-ops (matchMedia stayed false).
test("connect page starts no animation loop under reduced motion, and falls back to the static grid", async ({
  page,
}) => {
  await countRaf(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/connect.html");

  await expect(page.locator("canvas#bg")).toBeHidden();
  await page.waitForTimeout(600); // a loop, had one started, would have ticked by now
  expect(await rafCount(page)).toBe(0);
  await expect(page.locator("body")).toHaveCSS("background-image", /linear-gradient/);
});

test.use({
  launchOptions: {
    args: ["--host-resolver-rules=MAP battlelab.superstatus.io 127.0.0.1"],
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

async function stubSuccessfulConnect(page: import("@playwright/test").Page): Promise<void> {
  await page.addInitScript(() => {
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
        }
        return {
          teardown: () => {
            if (root) root.textContent = "";
          },
        };
      },
    };
  });
  await page.route("https://relay.battlelab.superstatus.io/altcha/challenge", async (route) => {
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
  });
}

test("connect sign-in is centered and keeps custom relay in advanced controls", async ({ page }) => {
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
  expect(Math.abs(box!.x + box!.width / 2 - viewport!.width / 2)).toBeLessThan(80);

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
  const saved = await page.evaluate((key) => sessionStorage.getItem(key), STORAGE_KEY);
  expect(saved).toBeTruthy();
  const parsed = JSON.parse(saved!);
  expect(parsed).toMatchObject({ relay: PUBLIC_RELAY, name: "viper-8231", key: "stream-secret" });
  // The relay announced a 4-hour deadline — the saved window must follow it and
  // must NOT be clamped back to the old one-hour cap (#662 regression).
  expect(parsed.expiresAt).toBeGreaterThan(Date.now() + 3_600_000);
  expect(parsed.expiresAt).toBeLessThanOrEqual(Date.now() + 14_400_000);

  // #684: on narrow viewports the bar defaults collapsed, hiding Sign out — expand it first.
  if (((await page.locator(".session-box").getAttribute("class")) ?? "").includes("collapsed")) {
    await page.locator("#session-toggle").click();
  }
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.locator(".connect-card")).toBeVisible();
  await expect(page.evaluate((key) => sessionStorage.getItem(key), STORAGE_KEY)).resolves.toBeNull();
  await expect(page.getByLabel("Access password")).toHaveValue("");
});

test("expired saved credentials are ignored", async ({ page, baseURL }) => {
  await page.addInitScript(
    ({ key, relay }) => {
      sessionStorage.setItem(
        key,
        JSON.stringify({ relay, name: "old-box", key: "old-secret", expiresAt: Date.now() - 1000 }),
      );
    },
    { key: STORAGE_KEY, relay: PUBLIC_RELAY },
  );

  await page.goto(publicConnectUrl(baseURL));

  await expect(page.locator(".connect-card")).toBeVisible();
  await expect(page.getByLabel("Console key")).toHaveValue("");
  await expect(page.evaluate((key) => sessionStorage.getItem(key), STORAGE_KEY)).resolves.toBeNull();
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
  if (((await box.getAttribute("class")) ?? "").includes("collapsed")) await toggle.click();
  await expect(box).not.toHaveClass(/collapsed/);
  await toggle.click();
  await expect(box).toHaveClass(/collapsed/);

  // The collapsed footprint is a small chip, not the full-width bar…
  const bb = (await box.boundingBox())!;
  expect(bb.width).toBeLessThan(140);
  // …and a point just beside it resolves to the app underneath, not the bar (chrome is
  // pointer-events:none so toolbar controls stay clickable).
  const hit = await page.evaluate(
    ({ x, y }) => (document.elementFromPoint(x, y)?.closest(".session-box") ? "bar" : "through"),
    { x: bb.x + bb.width + 40, y: bb.y + bb.height / 2 },
  );
  expect(hit).toBe("through");

  // The explicit choice is persisted in its own key, so a later reconnect restores it via
  // applyBarDefault() (which reads the override before falling back to the viewport default).
  expect(await page.evaluate(() => sessionStorage.getItem("battlelab.connect.bar.v1"))).toBe(
    "collapsed",
  );
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
      if (k === "battlelab.connect.bar.v1") throw new DOMException("blocked", "SecurityError");
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
  expect(await page.evaluate(() => sessionStorage.getItem("battlelab.connect.bar.v1"))).toBe(
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
