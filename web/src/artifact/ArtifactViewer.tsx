import { useEffect, useState } from "react";
import { ago, formatWhen, nowSeconds } from "../lib/shareFormat";
import { Atmosphere } from "../components/hud/Atmosphere";
import { Loader } from "../components/hud/Loader";
import { PublicBar } from "../components/public/PublicBar";
import styles from "../share/ShareViewer.module.css";
import own from "./ArtifactViewer.module.css";
import { GoneError, contentUrl, fetchMeta, publicIdFromPath, type ArtifactMeta } from "./publicArtifact";

type Phase =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "offline" }
  | { kind: "open"; meta: ArtifactMeta };

/** The sandbox tokens the server's `CSP: sandbox` grants the content (routes/artifacts.py
 *  SANDBOX_CSP). NEVER `allow-same-origin`: the framed page is an opaque origin, so it reaches no
 *  cookie, no storage and no `/api` of this BattleLab, and it can't reach up into this page. The
 *  frame and the header are two locks; either alone keeps the page out. */
export const FRAME_SANDBOX =
  "allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals";

/** A publicly shared artifact (#2068): what someone opening `/artifacts/p/<id>/` sees. The
 *  BattleLab header (title, "Artifact" chip, when it last changed) above the artifact itself,
 *  framed from `~/` and filling the rest of the page. Collapsing the header gives it the whole
 *  viewport. The server serves this page for ANY id; the one `~meta` read decides, and every
 *  failure of it is the same "this link no longer works", exactly as the server makes a revoked,
 *  unknown or disabled link indistinguishable. */
export function ArtifactViewer() {
  const id = publicIdFromPath(window.location.pathname);
  const [phase, setPhase] = useState<Phase>(id ? { kind: "loading" } : { kind: "unavailable" });

  useEffect(() => {
    if (!id) return;
    const ctl = new AbortController();
    fetchMeta(id, ctl.signal).then(
      (meta) => setPhase({ kind: "open", meta }),
      (e: unknown) => {
        if (ctl.signal.aborted) return;
        setPhase(e instanceof GoneError ? { kind: "unavailable" } : { kind: "offline" });
      },
    );
    return () => ctl.abort();
  }, [id]);

  const open = phase.kind === "open" ? phase.meta : null;
  const title = open?.title || "Shared artifact";
  useEffect(() => {
    document.title = `${title} · BattleLab`;
  }, [title]);

  return (
    <>
      <Atmosphere />
      <div className={styles.page} data-testid="artifact-viewer" data-phase={phase.kind}>
        <PublicBar>
          {open ? (
            <>
              <span className={styles.title}>{title}</span>
              <span className={styles.facts}>
                <span className={`${styles.chip} ${styles.chipMode}`}>
                  {open.kind === "site" ? "SITE" : "FILE"}
                </span>
                {open.updated_at > 0 && (
                  <span className={styles.chip} title={formatWhen(open.updated_at)}>
                    UPDATED {ago(open.updated_at, nowSeconds()).toUpperCase()}
                  </span>
                )}
              </span>
            </>
          ) : (
            <span className={styles.sub}>Shared artifact</span>
          )}
        </PublicBar>

        {phase.kind === "loading" && (
          <main className={styles.center}>
            <div className={styles.loading}>
              <Loader kind="radar" label="Opening the shared artifact…" />
            </div>
          </main>
        )}

        {phase.kind === "unavailable" && (
          <main className={styles.center}>
            <div className={styles.notice} data-testid="artifact-unavailable">
              <p className={styles.tag}>Link unavailable</p>
              <h1 className={styles.h1}>This link no longer works</h1>
              <p className={styles.muted}>
                It may have been revoked. Ask the person who shared it for a new link.
              </p>
            </div>
          </main>
        )}

        {phase.kind === "offline" && (
          <main className={styles.center}>
            <div className={styles.notice} data-testid="artifact-offline">
              <p className={styles.tag}>Can’t open right now</p>
              <h1 className={styles.h1}>This BattleLab can’t be reached</h1>
              <p className={styles.muted}>Try again in a moment.</p>
            </div>
          </main>
        )}

        {open && id && (
          <main className={own.main}>
            <iframe
              className={own.frame}
              src={contentUrl(id, window.location.search, window.location.hash)}
              sandbox={FRAME_SANDBOX}
              referrerPolicy="no-referrer"
              title={title}
              data-testid="artifact-frame"
            />
          </main>
        )}
      </div>
    </>
  );
}
