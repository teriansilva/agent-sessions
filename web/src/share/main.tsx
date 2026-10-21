import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import { ShareViewer } from "./ShareViewer";
import { applyMotion, prefersReducedMotion, resolveMotion } from "../theme/motion";

/** The public share viewer's entry (#1404 P4) — `share.html`, served at `/p/<token>` and `/p/`.
 *
 *  A separate bundle from the operator app: it registers no service worker, never reads the
 *  operator's session, and talks only to `/api/public/*` (`publicApi.ts`). No inline script in
 *  share.html, so the app's CSP (`script-src 'self'` + the index's own hashes) covers it as is.
 *
 *  The theme: the device's saved BattleLab choice when this browser has one (the operator opening
 *  their own link), else the system's light/dark preference. */
function bootViewerTheme(): void {
  let theme: string | null = null;
  try {
    theme = localStorage.getItem("tr-theme");
  } catch {
    /* storage blocked: fall back to the system preference */
  }
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  document.documentElement.setAttribute("data-theme", theme);
}

bootViewerTheme();
// The motion mode (#1531): a visitor's page has no stored setting; it follows the device. Every
// reduced-motion rule in the shared stylesheets keys off this attribute.
applyMotion(resolveMotion("system", prefersReducedMotion()));
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ShareViewer />
  </StrictMode>,
);
