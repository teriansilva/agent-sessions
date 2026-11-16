import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import { bootPageTheme } from "../theme/applyTheme";
import { applyMotion, prefersReducedMotion, resolveMotion } from "../theme/motion";
import { ArtifactViewer } from "./ArtifactViewer";

/** The public artifact viewer's entry (#2068): `artifact.html`, served at `/artifacts/p/<id>/`.
 *
 *  Its own small bundle, like the session-share and reply viewers: no service worker, no operator
 *  session, no cookie. It reads the link's `~meta` and frames the artifact's `~/` content in a
 *  sandbox. `artifactBundle.test.ts` keeps it that way. */
bootPageTheme();
applyMotion(resolveMotion("system", prefersReducedMotion()));
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ArtifactViewer />
  </StrictMode>,
);
