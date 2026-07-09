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
