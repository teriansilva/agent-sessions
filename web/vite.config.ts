import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

// https://vite.dev/config/
export default defineConfig({
  // Absolute base so /assets/* resolve correctly under deep client routes
  // (e.g. /s/claude/<uuid>) when FastAPI serves index.html as the SPA fallback.
  base: "/",
  // Release version stamped into the bundle (#661). install.sh's build_web() sets
  // AGENT_SESSIONS_VERSION from the just-installed package, so EVERY release changes dist
  // content → new SW precache revisions → the cached PWA shell refreshes even for
  // server-only releases. Unstamped (dev/CI) builds get "dev", which disables the
  // footer's version-mismatch prompt.
  define: {
    __APP_VERSION__: JSON.stringify(process.env.AGENT_SESSIONS_VERSION || "dev"),
  },
  preview: {
    // The connect-page E2E maps battlelab.superstatus.io to the local preview server so the
    // public-host credential behavior is exercised in a real browser.
    allowedHosts: ["battlelab.superstatus.io"],
  },
  // A modern cssTarget so esbuild keeps `backdrop-filter` as authored. With the default
  // cssTarget (which includes old Safari) esbuild collapses a dual `backdrop-filter` +
  // `-webkit-backdrop-filter` declaration to the `-webkit-` form ONLY and DROPS the standard
  // property — so the frost didn't render where the unprefixed property is needed and DevTools
  // flagged the prefixed-only rule. These targets all support unprefixed `backdrop-filter`, so
  // the standard property survives; the redundant hand-written `-webkit-backdrop-filter`
  // declarations were removed from the CSS (the `@supports` feature tests stay).
  build: {
    outDir: "dist",
    cssTarget: ["chrome111", "firefox128", "safari18", "edge111"],
    // Multi-entry: the SPA (index.html) + the standalone Home Free connect page
    // (connect.html). connect.html is NOT linked from the app — a dark page for
    // reaching a box through the blind relay.
    rollupOptions: {
      input: {
        main: "index.html",
        connect: "connect.html",
      },
    },
  },
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      // injectManifest, not generateSW (#726 Phase 3): Web Push needs a `push` listener, and
      // generateSW offers no seam to add one. The hand-written worker in src/sw.ts reproduces
      // the previous behaviour exactly — precache the shell, navigation-fallback to
      // index.html, deny that fallback for every server-rendered route. The denylist moved to
      // src/sw-denylist.ts so the SW and its test share one source of truth; dropping an entry
      // there is silent at build AND runtime, and shows up only as someone locked out.
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      // Registration is explicit (initSWUpdates() in main.tsx via virtual:pwa-register) so a
      // long-lived tab re-checks sw.js hourly + on foreground (#661) — don't ALSO inject the
      // one-shot registerSW.js script.
      injectRegister: null,
      // Precache the built static shell ONLY. Live data + the terminal stay
      // network-only: the SPA navigation fallback explicitly excludes the API,
      // websocket, terminal, auth, and upload paths so they're never served stale
      // from cache (per #64 PWA rule). No runtimeCaching entries for them on purpose.
      injectManifest: {
        globPatterns: ["**/*.{js,css,html,svg,png,woff2}"],
      },
      manifest: {
        name: "BattleLab",
        short_name: "BattleLab",
        description: "BattleLab — a self-hosted command deck for your AI-coding agents.",
        theme_color: "#0d0e10",
        background_color: "#0d0e10",
        display: "standalone",
        start_url: "./",
        icons: [
          { src: "favicon.svg", sizes: "any", type: "image/svg+xml", purpose: "any" },
          { src: "icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
          { src: "icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
          {
            src: "icon-maskable-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
    }),
  ],
});
