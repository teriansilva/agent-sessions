import { defineConfig, devices } from "@playwright/test";

// A dedicated port, not the VitePress default: a stray `preview` left running by another checkout
// will happily answer on the default and serve that checkout's build, so the suite passes against
// code you are not testing. Binding somewhere specific and refusing to reuse an existing server
// makes that failure impossible rather than merely unlikely.
const PORT = Number(process.env.DOCS_E2E_PORT ?? 4183);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // one static server, and the suite is small
  reporter: process.env.CI ? "line" : "list",
  use: { baseURL: `http://127.0.0.1:${PORT}` },
  projects: [
    { name: "mobile", use: { ...devices["Pixel 7"], viewport: { width: 414, height: 900 } } },
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
  ],
  webServer: {
    command: `npx vitepress preview --host 127.0.0.1 --port ${PORT}`,
    port: PORT,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
