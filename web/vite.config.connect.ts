import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Standalone build of the Home Free connect page for public hosting at
// battlelab.superstatus.io/connect/ — deployed to the landing edge, NOT served by the app.
//
// This intentionally excludes VitePWA: /connect/ must be plain static HTML + hashed assets, with
// no service worker under the landing origin. Public assets stay enabled because app-mode
// dynamic-imports the real SPA and routes like onboarding reference /onboarding/*.svg.
export default defineConfig({
  base: "/connect/",
  publicDir: "public",
  // No PWA plugin here, but app-mode's dynamic import of the real SPA reaches swUpdate.ts,
  // which imports `virtual:pwa-register` (#661) — a module only the PWA plugin provides.
  // Alias it to the no-op stub so the connect build resolves; nothing registers, keeping the
  // "no SW artifacts in dist-connect" invariant the deploy workflow asserts.
  resolve: {
    alias: {
      "virtual:pwa-register": new URL("./src/lib/pwaRegisterStub.ts", import.meta.url).pathname,
    },
  },
  // The SPA's version stamp (#661): harmless for /connect/ itself, but the imported SPA
  // modules reference __APP_VERSION__ and the define must exist for the build to resolve it.
  define: {
    __APP_VERSION__: JSON.stringify(process.env.AGENT_SESSIONS_VERSION || "dev"),
  },
  build: {
    outDir: "dist-connect",
    emptyOutDir: true,
    cssTarget: ["chrome111", "firefox128", "safari18", "edge111"],
    rollupOptions: {
      input: { connect: "connect.html" },
    },
  },
  plugins: [react()],
});
