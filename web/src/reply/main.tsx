import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import { bootPageTheme } from "../theme/applyTheme";
import { applyMotion, prefersReducedMotion, resolveMotion } from "../theme/motion";
import { ReplyViewer } from "./ReplyViewer";

/** The reply-link viewer's entry (#1956): `reply.html`, served at `/r/<id>`.
 *
 *  Its own small bundle, like the session-share viewer: no service worker, no operator session, no
 *  cookie. It fetches one sealed envelope and opens it with the key from the link's fragment.
 *  `replyBundle.test.ts` keeps it that way. */
bootPageTheme();
applyMotion(resolveMotion("system", prefersReducedMotion()));
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ReplyViewer />
  </StrictMode>,
);
