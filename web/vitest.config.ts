import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Unit + component tests (vitest + jsdom + RTL). The Playwright e2e specs in
// e2e/ are excluded — they run via `npm run test:e2e`, not here.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    include: ["src/**/*.{test,spec}.{ts,tsx}", "visual/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
  },
});
