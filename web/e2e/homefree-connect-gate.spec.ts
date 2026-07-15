import { expect, test } from "@playwright/test";

import { verifyHuman } from "./connect-helpers";

// #690 human-verification gate: the connect page must be inert to scripts. The contract is
// network-observable — zero /altcha/challenge fetches and zero WebSocket constructions before a
// fresh press-and-hold verification, exactly one attempt after it, and a re-gate on every
// sign-out/failure/reload. Real browser required: this is pointer/keyboard event semantics.

test.use({
  launchOptions: {
    args: ["--host-resolver-rules=MAP battlelab.superstatus.io 127.0.0.1"],
  },
});

const STORAGE_KEY = "battlelab.connect.session.v1";
const ZERO_ALTCHA = "5feceb66ffc86f38d952786c6d696c79c2dbc239dd4e91b46729d73a27fb57e9";

function publicConnectUrl(baseURL: string | undefined): string {
  const u = new URL(baseURL ?? "http://localhost:41873");
  return `http://battlelab.superstatus.io:${u.port}/connect.html`;
}

/** Count /altcha/challenge requests — the first network side effect of a connect attempt. */
function trackAltcha(page: import("@playwright/test").Page): () => number {
  let n = 0;
  page.on("request", (req) => {
    if (req.url().includes("/altcha/challenge")) n++;
  });
  return () => n;
}

const wsCount = (page: import("@playwright/test").Page) =>
  page.evaluate(() => (window as unknown as { __wsCount: number }).__wsCount);

/**
 * Instrumented harness: counts WebSocket constructions, shortens the hold, and either
 * completes or fails the mount. The ALTCHA route is stubbed so a post-verification attempt
 * can proceed without a live relay.
 */
async function stubInstrumentedConnect(
  page: import("@playwright/test").Page,
  opts: { failMount?: boolean; holdMs?: number } = {},
): Promise<void> {
  await page.addInitScript(
    ({ failMount, holdMs }) => {
      (window as unknown as { __wsCount: number }).__wsCount = 0;
      window.__battlelabConnectHarness = {
        holdMs,
        makeWebSocket: () => {
          (window as unknown as { __wsCount: number }).__wsCount++;
          return {
            binaryType: "arraybuffer",
            onopen: null,
            onmessage: null,
            onclose: null,
            onerror: null,
            readyState: 1,
            send() {},
            close() {},
          };
        },
        mountApp: async (_ws, _key, _captcha, mountOpts) => {
          if (failMount) throw new Error("mount failed (test)");
          mountOpts?.onEvent?.({
            type: "paired",
            deadline: Math.floor(Date.now() / 1000) + 14400,
            ttl: 14400,
          });
          const root = document.getElementById("app-root");
          if (root) root.textContent = "Mock streamed BattleLab app";
          return {
            teardown: () => {
              if (root) root.textContent = "";
            },
          };
        },
      };
    },
    { failMount: opts.failMount ?? false, holdMs: opts.holdMs ?? 250 },
  );
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

async function fillCredentials(page: import("@playwright/test").Page): Promise<void> {
  await page.getByLabel("Console key").fill("nightjar-1010");
  await page.getByLabel("Access password").fill("stream-secret");
}

test("scripted connects are inert: zero challenge fetches and zero sockets before verification", async ({
  page,
  baseURL,
}) => {
  const altcha = trackAltcha(page);
  await stubInstrumentedConnect(page);
  await page.goto(publicConnectUrl(baseURL));
  await fillCredentials(page);

  // The button is disabled pre-verification…
  await expect(page.getByRole("button", { name: "Connect" })).toBeDisabled();

  // …and even a JS-driven submit that bypasses the disabled button goes nowhere.
  await page.evaluate(() =>
    (document.getElementById("connect-form") as HTMLFormElement).requestSubmit(),
  );
  await expect(page.locator("#status")).toContainText(/hold to verify/i);
  await expect(page.locator("#status")).toHaveAttribute("data-kind", "error");
  await expect(page.locator("#verify-gate")).toHaveAttribute("data-state", "idle");
  expect(altcha()).toBe(0);
  expect(await wsCount(page)).toBe(0);
});

test("interrupting the hold cancels it: early release, window blur, and auto-repeat are all inert", async ({
  page,
  baseURL,
}) => {
  const altcha = trackAltcha(page);
  await stubInstrumentedConnect(page, { holdMs: 1200 });
  await page.goto(publicConnectUrl(baseURL));
  await fillCredentials(page);
  const gate = page.locator("#verify-gate");
  const hold = page.locator("#gate-hold");

  // Early release: progress resets to idle, Connect stays disabled.
  await hold.hover();
  await page.mouse.down();
  await expect(gate).toHaveAttribute("data-state", "holding");
  await page.waitForTimeout(250);
  await page.mouse.up();
  await expect(gate).toHaveAttribute("data-state", "idle");
  await expect(page.locator("#gate-meter")).toHaveAttribute("aria-valuenow", "0");
  await expect(page.getByRole("button", { name: "Connect" })).toBeDisabled();

  // Window blur mid-hold cancels too (tab-away must not accumulate progress).
  await page.mouse.down();
  await expect(gate).toHaveAttribute("data-state", "holding");
  await page.evaluate(() => window.dispatchEvent(new Event("blur")));
  await expect(gate).toHaveAttribute("data-state", "idle");
  await page.mouse.up();

  // OS keyboard auto-repeat is not a sustained press — a repeat keydown starts nothing.
  await hold.dispatchEvent("keydown", { key: " ", repeat: true });
  await expect(gate).toHaveAttribute("data-state", "idle");

  expect(altcha()).toBe(0);
  expect(await wsCount(page)).toBe(0);
});

test("a completed hold arms exactly one attempt, and sign-out re-gates", async ({
  page,
  baseURL,
}) => {
  const altcha = trackAltcha(page);
  await stubInstrumentedConnect(page);
  await page.goto(publicConnectUrl(baseURL));
  await fillCredentials(page);

  await verifyHuman(page);
  // Verified: the one-way control is no longer actionable (no toggle semantics).
  await expect(page.locator("#gate-hold")).toBeDisabled();
  const connect = page.getByRole("button", { name: "Connect" });
  await expect(connect).toBeEnabled();

  await connect.click();
  await expect(page.locator(".session-box")).toBeVisible();
  expect(altcha()).toBe(1);
  expect(await wsCount(page)).toBe(1);

  // Sign out (the bar may default collapsed on narrow viewports) → the gate is fresh again.
  if (((await page.locator(".session-box").getAttribute("class")) ?? "").includes("collapsed")) {
    await page.locator("#session-toggle").click();
  }
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.locator(".connect-card")).toBeVisible();
  await expect(page.locator("#verify-gate")).toHaveAttribute("data-state", "idle");
  await expect(page.getByRole("button", { name: "Connect" })).toBeDisabled();

  // The consumed verification cannot be replayed by a scripted re-submit.
  await fillCredentials(page);
  await page.evaluate(() =>
    (document.getElementById("connect-form") as HTMLFormElement).requestSubmit(),
  );
  await expect(page.locator("#status")).toContainText(/hold to verify/i);
  expect(altcha()).toBe(1);
  expect(await wsCount(page)).toBe(1);
});

test("a failed connect re-gates the next attempt", async ({ page, baseURL }) => {
  const altcha = trackAltcha(page);
  await stubInstrumentedConnect(page, { failMount: true });
  await page.goto(publicConnectUrl(baseURL));
  await fillCredentials(page);

  await verifyHuman(page);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.locator("#status")).toContainText(/could not connect/i);
  expect(altcha()).toBe(1);

  // The failure consumed the verification: back to an idle gate and a disabled button…
  await expect(page.locator("#verify-gate")).toHaveAttribute("data-state", "idle");
  await expect(page.getByRole("button", { name: "Connect" })).toBeDisabled();

  // …and a fresh hold recovers the flow.
  await verifyHuman(page);
  await expect(page.getByRole("button", { name: "Connect" })).toBeEnabled();
});

test("a reload with saved credentials stops at a fresh gate — no auto-connect", async ({
  page,
  baseURL,
}) => {
  const altcha = trackAltcha(page);
  await stubInstrumentedConnect(page);
  await page.addInitScript(
    ({ key }) => {
      sessionStorage.setItem(
        key,
        JSON.stringify({
          relay: "https://relay.battlelab.superstatus.io",
          name: "saved-box",
          key: "saved-secret",
          expiresAt: Date.now() + 3_600_000,
        }),
      );
    },
    { key: STORAGE_KEY },
  );

  await page.goto(publicConnectUrl(baseURL));

  // Credentials restore into the form, but the page must NOT auto-connect (#690):
  await expect(page.locator(".connect-card")).toBeVisible();
  await expect(page.getByLabel("Console key")).toHaveValue("saved-box");
  await expect(page.locator("#status")).toContainText(/hold to verify/i);
  await expect(page.locator("#verify-gate")).toHaveAttribute("data-state", "idle");
  await expect(page.getByRole("button", { name: "Connect" })).toBeDisabled();

  await page.waitForTimeout(600); // an auto-connect, had one started, would have fetched by now
  expect(altcha()).toBe(0);
  expect(await wsCount(page)).toBe(0);

  // The restored session still connects fine after a genuine hold.
  await verifyHuman(page);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.locator(".session-box")).toBeVisible();
  expect(altcha()).toBe(1);
});

test("a lost key release cannot verify, and a cancelled hold never latches the keyboard", async ({
  page,
  baseURL,
}) => {
  const altcha = trackAltcha(page);
  await stubInstrumentedConnect(page, { holdMs: 400 });
  await page.goto(publicConnectUrl(baseURL));
  await fillCredentials(page);
  const gate = page.locator("#verify-gate");
  const hold = page.locator("#gate-hold");

  // Focus escapes mid-hold: press Space on the button, move focus before releasing.
  // The hold must cancel immediately — the timer must NOT run on to "verified"
  // after the physical key was released somewhere else.
  await hold.focus();
  await page.keyboard.down(" ");
  await expect(gate).toHaveAttribute("data-state", "holding");
  await page.locator("#name").focus();
  await expect(gate).toHaveAttribute("data-state", "idle");
  await page.waitForTimeout(600); // > holdMs — a still-running timer would have verified
  await expect(gate).toHaveAttribute("data-state", "idle");
  await page.keyboard.up(" "); // the release lands on the input, not the button
  await expect(page.getByRole("button", { name: "Connect" })).toBeDisabled();

  // Blur cancels mid-hold and the release lands elsewhere: the key latch must clear,
  // or every later keyboard hold would be ignored.
  await hold.focus();
  await page.keyboard.down(" ");
  await expect(gate).toHaveAttribute("data-state", "holding");
  await page.evaluate(() => window.dispatchEvent(new Event("blur")));
  await expect(gate).toHaveAttribute("data-state", "idle");
  await page.keyboard.up(" ");

  // A fresh keyboard hold still verifies — the gate is not latched shut.
  await hold.focus();
  await page.keyboard.down(" ");
  await expect(gate).toHaveAttribute("data-state", "verified", { timeout: 5_000 });
  await page.keyboard.up(" ");
  await expect(page.getByRole("button", { name: "Connect" })).toBeEnabled();
  expect(altcha()).toBe(0);
  expect(await wsCount(page)).toBe(0);
});

test("the gate is keyboard-operable and its progress survives reduced motion", async ({
  page,
  baseURL,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await stubInstrumentedConnect(page, { holdMs: 600 });
  await page.goto(publicConnectUrl(baseURL));
  await fillCredentials(page);
  const gate = page.locator("#verify-gate");
  const hold = page.locator("#gate-hold");
  const meter = page.locator("#gate-meter");

  // Thumb-sized target (docs/design.md §8) with an accessible progressbar.
  const box = (await hold.boundingBox())!;
  expect(box.height).toBeGreaterThanOrEqual(44);
  await expect(meter).toHaveAttribute("aria-labelledby", "gate-title");

  // Keyboard parity: a sustained Space press drives the same hold, with visible progress
  // even under reduced motion (the meter is state, not a decorative animation).
  await hold.focus();
  await page.keyboard.down(" ");
  await expect(gate).toHaveAttribute("data-state", "holding");
  await expect
    .poll(async () => Number(await meter.getAttribute("aria-valuenow")))
    .toBeGreaterThan(0);
  await expect
    .poll(() => page.locator("#gate-fill").evaluate((el) => el.getBoundingClientRect().width))
    .toBeGreaterThan(0);
  await expect(gate).toHaveAttribute("data-state", "verified", { timeout: 5_000 });
  await page.keyboard.up(" ");

  await expect(meter).toHaveAttribute("aria-valuenow", "100");
  await expect(hold).toContainText(/verified — human/i);
  await expect(page.getByRole("button", { name: "Connect" })).toBeEnabled();
});
