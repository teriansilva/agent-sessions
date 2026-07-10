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
