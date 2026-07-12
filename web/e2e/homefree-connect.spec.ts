import { expect, test } from "@playwright/test";

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

  // Corner-bracket frame, not a rounded box.
  await expect(page.locator(".connect-card .hud-cnr")).toHaveCount(4);
  await expect(page.locator(".connect-card")).toHaveCSS("border-radius", "0px");

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
          deadline: Math.floor(Date.now() / 1000) + 3500,
          ttl: 3600,
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

test("public connect signs in, canonicalizes the URL, stores credentials for the hour, and signs out", async ({
  page,
  baseURL,
}) => {
  await stubSuccessfulConnect(page);
  await page.goto(publicConnectUrl(baseURL));

  await expect(page.getByLabel("Relay base URL")).toBeHidden();
  await page.getByLabel("Console key").fill("viper-8231");
  await page.getByLabel("Access password").fill("stream-secret");
  await page.getByRole("button", { name: "Connect" }).click();

  await expect(page.locator(".session-box")).toBeVisible();
  await expect(page.locator(".connect-card")).toBeHidden();
  await expect(page).toHaveURL(/\/connect\/$/);
  const saved = await page.evaluate((key) => sessionStorage.getItem(key), STORAGE_KEY);
  expect(saved).toBeTruthy();
  const parsed = JSON.parse(saved!);
  expect(parsed).toMatchObject({ relay: PUBLIC_RELAY, name: "viper-8231", key: "stream-secret" });
  expect(parsed.expiresAt).toBeGreaterThan(Date.now());
  expect(parsed.expiresAt).toBeLessThanOrEqual(Date.now() + 3_600_000);

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

test("connected management floats at the upper center on mobile and desktop", async ({
  page,
  baseURL,
}) => {
  await stubSuccessfulConnect(page);
  await page.goto(publicConnectUrl(baseURL));
  await page.getByLabel("Console key").fill("nightjar-1010");
  await page.getByLabel("Access password").fill("stream-secret");
  await page.getByRole("button", { name: "Connect" }).click();

  const controls = page.locator(".session-box");
  await expect(controls).toBeVisible();
  const box = await controls.boundingBox();
  const viewport = page.viewportSize();
  expect(box).not.toBeNull();
  expect(viewport).not.toBeNull();
  expect(box!.y).toBeLessThan(28);
  expect(Math.abs(box!.x + box!.width / 2 - viewport!.width / 2)).toBeLessThan(32);
  expect(box!.width).toBeLessThanOrEqual(viewport!.width - 16);
});
