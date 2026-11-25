import { useEffect, useRef, useState } from "react";
import { ago, formatWhen, nowSeconds } from "../lib/shareFormat";
import { Atmosphere } from "../components/hud/Atmosphere";
import { Loader } from "../components/hud/Loader";
import { PublicBar } from "../components/public/PublicBar";
import styles from "../share/ShareViewer.module.css";
import ownCss from "./ArtifactViewer.module.css";
import {
  GoneError,
  LockedError,
  UnlockError,
  contentUrl,
  fetchMeta,
  fetchOwnMeta,
  ownContentUrl,
  ownTokenFromPath,
  publicIdFromPath,
  unlock,
  type ArtifactMeta,
} from "./publicArtifact";

type Phase =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "offline" }
  /** A password link (#2217): the server said only whether the page wears the header. */
  | { kind: "locked"; header: boolean; error: string | null; busy: boolean }
  /** `token` is a password link's unlock token, held in memory only. */
  | { kind: "open"; meta: ArtifactMeta; token?: string };

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
 *  expired, unknown or disabled link indistinguishable.
 *
 *  A password link (#2217) shows its password card first, nothing of the artifact; the unlock
 *  answers an unlock token the frame's address carries. A link shared without the header is the
 *  artifact edge to edge, under its own title.
 *
 *  The operator's own view (#2270), `/artifacts/o/<view token>/` where a signed-in Open lands, is
 *  the same page: its `~meta` is authorised by the view token in the path, it ALWAYS wears the
 *  header (hiding it is a share option, and this is not a share), and it frames the artifact
 *  under that token. */
export function ArtifactViewer() {
  const id = publicIdFromPath(window.location.pathname);
  const own = id ? null : ownTokenFromPath(window.location.pathname);
  const [phase, setPhase] = useState<Phase>(
    id || own ? { kind: "loading" } : { kind: "unavailable" },
  );
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const unlocking = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!id && !own) return;
    const ctl = new AbortController();
    (id ? fetchMeta(id, ctl.signal) : fetchOwnMeta(own!, ctl.signal)).then(
      (meta) => setPhase({ kind: "open", meta }),
      (e: unknown) => {
        if (ctl.signal.aborted) return;
        if (e instanceof LockedError) {
          setPhase({
            kind: "locked",
            header: e.header,
            error: null,
            busy: false,
          });
          return;
        }
        setPhase(
          e instanceof GoneError
            ? { kind: "unavailable" }
            : { kind: "offline" },
        );
      },
    );
    return () => {
      ctl.abort();
      unlocking.current?.abort();
    };
  }, [id, own]);

  const submitPassword = async () => {
    if (!id || !password || phase.kind !== "locked" || phase.busy) return;
    const header = phase.header;
    setPhase({ kind: "locked", header, error: null, busy: true });
    const ctl = new AbortController();
    unlocking.current = ctl;
    try {
      const { meta, token } = await unlock(id, password, ctl.signal);
      setPassword("");
      setPhase({ kind: "open", meta, token });
    } catch (e) {
      if (ctl.signal.aborted) return;
      if (e instanceof GoneError) return setPhase({ kind: "unavailable" });
      let error = "This BattleLab can’t be reached. Try again in a moment.";
      if (e instanceof UnlockError) {
        const left = e.triesLeft;
        error = e.paused
          ? "Too many wrong passwords. Try again in 15 minutes."
          : left !== null && left > 0
            ? `Wrong password. ${left} ${left === 1 ? "try" : "tries"} left before a 15-minute pause.`
            : "Wrong password.";
      }
      setPhase({ kind: "locked", header, error, busy: false });
    }
  };

  const open = phase.kind === "open" ? phase.meta : null;
  const title = open?.title || (own ? "Artifact" : "Shared artifact");
  // Without the header (#2217) the page is the artifact's own: its title alone, no BattleLab bar,
  // no atmosphere. While the first read is in flight nothing is drawn, so a headerless link never
  // flashes a bar; a link that no longer works keeps it (it is BattleLab's own answer).
  const bare =
    phase.kind === "loading" ||
    (phase.kind === "open" && !phase.meta.header) ||
    (phase.kind === "locked" && !phase.header);
  useEffect(() => {
    if (phase.kind === "loading") return;
    document.title = open && !open.header ? title : `${title} · BattleLab`;
  }, [title, open, phase.kind]);

  return (
    <>
      {!bare && <Atmosphere />}
      <div
        className={styles.page}
        data-testid="artifact-viewer"
        data-phase={phase.kind}
        data-header={bare ? "off" : "on"}
      >
        {!bare && (
          <PublicBar>
            {open ? (
              <>
                <span className={styles.title}>{title}</span>
                <span className={styles.facts}>
                  <span className={`${styles.chip} ${styles.chipMode}`}>
                    {open.kind === "site" ? "SITE" : "FILE"}
                  </span>
                  {open.updated_at > 0 && (
                    <span
                      className={styles.chip}
                      title={formatWhen(open.updated_at)}
                    >
                      UPDATED {ago(open.updated_at, nowSeconds()).toUpperCase()}
                    </span>
                  )}
                </span>
              </>
            ) : (
              <span className={styles.sub}>
                {own ? "Artifact" : "Shared artifact"}
              </span>
            )}
          </PublicBar>
        )}

        {phase.kind === "locked" && (
          <main className={styles.center}>
            <form
              className={styles.card}
              data-testid="artifact-password-gate"
              onSubmit={(e) => {
                e.preventDefault();
                void submitPassword();
              }}
            >
              <p className={styles.tag}>Password required</p>
              <h1 className={styles.h1}>This link needs a password</h1>
              <p className={styles.muted}>Ask the person who shared it.</p>
              <label className={styles.label} htmlFor="artifact-pw">
                Password
              </label>
              <div className={styles.pwRow}>
                <input
                  id="artifact-pw"
                  className={styles.input}
                  type={showPw ? "text" : "password"}
                  autoComplete="current-password"
                  autoFocus
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
                <button
                  type="button"
                  className={styles.ghost}
                  aria-pressed={showPw}
                  onClick={() => setShowPw((v) => !v)}
                >
                  {showPw ? "Hide" : "Show"}
                </button>
              </div>
              {phase.error && (
                <p className={styles.error} role="alert">
                  {phase.error}
                </p>
              )}
              <button
                type="submit"
                className={styles.primary}
                disabled={phase.busy || !password}
              >
                {phase.busy ? "Opening…" : "Open"}
              </button>
            </form>
          </main>
        )}

        {phase.kind === "loading" && (
          <main className={styles.center}>
            <div className={styles.loading}>
              <Loader
                kind="radar"
                label={
                  own ? "Opening the artifact…" : "Opening the shared artifact…"
                }
              />
            </div>
          </main>
        )}

        {phase.kind === "unavailable" && (
          <main className={styles.center}>
            <div className={styles.notice} data-testid="artifact-unavailable">
              {own ? (
                <>
                  <p className={styles.tag}>View expired</p>
                  <h1 className={styles.h1}>This view has expired</h1>
                  <p className={styles.muted}>
                    Open the artifact again from BattleLab.
                  </p>
                </>
              ) : (
                <>
                  <p className={styles.tag}>Link unavailable</p>
                  <h1 className={styles.h1}>This link no longer works</h1>
                  <p className={styles.muted}>
                    It may have been revoked. Ask the person who shared it for a
                    new link.
                  </p>
                </>
              )}
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

        {open && (id || own) && (
          <main className={ownCss.main}>
            <iframe
              className={ownCss.frame}
              src={
                id
                  ? contentUrl(
                      id,
                      window.location.search,
                      window.location.hash,
                      phase.kind === "open" ? phase.token : undefined,
                    )
                  : ownContentUrl(
                      own!,
                      window.location.search,
                      window.location.hash,
                    )
              }
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
