import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/** The hosted Markdown renderer (#2210): ONE classic script and ONE stylesheet, written beside the
 *  app's assets as `dist/assets/artifact-md.js` + `artifact-md.css` (run after the app build, which
 *  empties `dist/`). A Markdown artifact's page runs in an opaque-origin sandbox, so it may load a
 *  classic `<script src>` and a stylesheet (no-cors), but a module script and every chunk it imports
 *  are CORS requests, which BattleLab never answers. Hence library mode, IIFE, everything inlined.
 *  The server links the files with a content hash (`routes/artifacts.py`), so a fixed name is never
 *  stale. Not precached: the app's service worker is built before this runs. */
export default defineConfig({
  plugins: [react()],
  // Library mode leaves `process.env.NODE_ENV` alone, and React reads it: without this the
  // script throws in the browser.
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  publicDir: false,
  build: {
    outDir: "dist/assets",
    emptyOutDir: false,
    copyPublicDir: false,
    cssTarget: ["chrome111", "firefox128", "safari18", "edge111"],
    lib: {
      entry: "src/artifact-md/main.tsx",
      formats: ["iife"],
      name: "BattleLabArtifactMarkdown",
      fileName: () => "artifact-md.js",
      cssFileName: "artifact-md",
    },
  },
});
