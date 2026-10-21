/// <reference types="vite-plugin-pwa/client" />

/** Build-time release stamp (#661): `AGENT_SESSIONS_VERSION` at build, `"dev"` when unstamped.
 *  Injected via `define` in vite.config.ts (app builds) and vitest.config.ts (tests). */
declare const __APP_VERSION__: string;
