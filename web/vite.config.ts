import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

// https://vite.dev/config/
export default defineConfig({
  // Absolute base so /assets/* resolve correctly under deep client routes
  // (e.g. /s/claude/<uuid>) when FastAPI serves index.html as the SPA fallback.
  base: "/",
  build: { outDir: "dist" },
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      // Precache the built static shell ONLY. Live data + the terminal stay
      // network-only: the SPA navigation fallback explicitly excludes the API,
      // websocket, terminal, auth, and upload paths so they're never served stale
      // from cache (per #64 PWA rule). No runtimeCaching entries for them on purpose.
      workbox: {
        navigateFallback: "index.html",
        // Server-rendered (Jinja) routes the SPA must NOT shadow with its index.html
        // fallback — incl. /change-password (the forced first-login change has no SPA
        // route; without this the SW serves the React shell there and login dead-ends).
        navigateFallbackDenylist: [
          /^\/api/,
          /^\/ws/,
          /^\/term/,
          /^\/login/,
          /^\/logout/,
          /^\/change-password/,
          /^\/healthz/,
        ],
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
