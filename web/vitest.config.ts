import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Unit + component tests (vitest + jsdom + RTL). The Playwright e2e specs in
// e2e/ are excluded — they run via `npm run test:e2e`, not here.
export default defineConfig({
  plugins: [react()],
  // Tests run as an unstamped build (#661) — the hook accepts an injected version where a
  // test needs a stamped one. The PWA plugin's virtual module doesn't exist under vitest,
  // so it resolves to a no-op stub.
  define: {
    __APP_VERSION__: JSON.stringify("dev"),
  },
  resolve: {
    alias: {
      "virtual:pwa-register": new URL("./src/lib/pwaRegisterStub.ts", import.meta.url).pathname,
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    include: ["src/**/*.{test,spec}.{ts,tsx}", "visual/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
  },
});
