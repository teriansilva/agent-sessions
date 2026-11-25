import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import { ShareViewer } from "./ShareViewer";
import { bootPageTheme } from "../theme/applyTheme";
import { applyMotion, prefersReducedMotion, resolveMotion } from "../theme/motion";

/** The public share viewer's entry (#1404 P4) — `share.html`, served at `/p/<token>` and `/p/`.
 *
 *  A separate bundle from the operator app: it registers no service worker, never reads the
 *  operator's session, and talks only to `/api/public/*` (`publicApi.ts`). No inline script in
 *  share.html, so the app's CSP (`script-src 'self'` + the index's own hashes) covers it as is.
 *
 *  The theme: the device's saved BattleLab choice when this browser has one (the operator opening
 *  their own link), else the system's light/dark preference (theme/applyTheme `bootPageTheme`). */

bootPageTheme();
// The motion mode (#1531): a visitor's page has no stored setting; it follows the device. Every
// reduced-motion rule in the shared stylesheets keys off this attribute. Set here before the first
// paint; ShareViewer keeps it live (followDeviceMotion) and owns the hidden-tab / idle pause.
applyMotion(resolveMotion("system", prefersReducedMotion()));
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ShareViewer />
  </StrictMode>,
);
