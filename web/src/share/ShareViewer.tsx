import { useCallback, useEffect, useRef, useState } from "react";
import { expiryChip, modeChip, nowSeconds } from "../lib/shareFormat";
import { ApiView } from "./ApiView";
import {
  PublicError,
  VIEWER_PATH,
  publicApi,
  tokenFromPath,
  type Unlocked,
} from "./publicApi";
import styles from "./ShareViewer.module.css";
import { BrandMark } from "../components/brand/Brand";
import { Atmosphere } from "../components/hud/Atmosphere";
import { Loader } from "../components/hud/Loader";
import { followDeviceMotion } from "../theme/motion";

type Phase =
  | { kind: "loading" }
  | { kind: "password"; error: string | null; busy: boolean }
  | { kind: "unavailable" }
  | { kind: "relink" }
  | { kind: "nocookie" }
  | { kind: "offline"; message: string }
  | { kind: "open"; unlocked: Unlocked };

/** The public viewer (#1404 P4): what someone opening `/p/<token>` sees.
 *
 *  1. The token in the address bar is traded for a share cookie (`POST /api/public/unlock`), with
 *     the password when the link has one.
 *  2. On success the address bar moves to `/p/` (`history.replaceState`): the token no longer sits
 *     in the bar, and the current back/forward entry is rewritten. (The browser's own history of
 *     visited URLs still holds the link it was opened with — nothing a page can change.) A reload
 *     on `/p/` asks `GET /api/public/share`, which the cookie answers. The token is kept in memory
 *     only, to unlock again if the 12 h cookie runs out — at most once a minute, so a browser that
 *     refuses the cookie gets a plain explanation instead of an unlock loop.
 *  3. Any 404 is the same "this link no longer works" — revoked, expired, wrong, or the session is
 *     gone — exactly as the server makes them indistinguishable. */
/** A re-unlock sooner than this after the last one means the cookie isn't being kept. */
const REUNLOCK_MIN_MS = 60_000;

/** `/p/<token>` or the bare `/p/` the viewer moves to. Anything else under `/p/` (a link a chat app
 *  mangled with a trailing `.` or `)`) is a link that can't work, not one to "open again". */
function mangled(pathname: string): boolean {
  return !tokenFromPath(pathname) && !/^\/p\/?$/.test(pathname);
}

export function ShareViewer() {
  const token = useRef<string | null>(tokenFromPath(window.location.pathname));
  const [broken] = useState(() => mangled(window.location.pathname));
  const lastUnlock = useRef(0);
  const lastName = useRef<string | null>(null);
  // The atmosphere's motion (#1596): no stored setting on this page, so the mode follows the
  // device's reduced-motion setting LIVE, and the viewer owns the hidden-tab / idle pause
  // (`data-motion-paused`) — what MotionProvider does in the SPA. main.tsx sets the mode pre-paint.
  useEffect(() => followDeviceMotion(), []);
  const [phase, setPhase] = useState<Phase>(() =>
    mangled(window.location.pathname)
      ? { kind: "unavailable" }
      : { kind: "loading" },
  );
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);

  const opened = useCallback((unlocked: Unlocked, viaToken = false) => {
    if (viaToken) lastUnlock.current = Date.now();
    lastName.current = unlocked.share.name;
    if (window.location.pathname !== VIEWER_PATH) {
      window.history.replaceState(null, "", VIEWER_PATH);
    }
    setPassword("");
    setPhase({ kind: "open", unlocked });
  }, []);

  const failed = useCallback((e: unknown) => {
    if (e instanceof PublicError) {
      if (e.status === 404) return setPhase({ kind: "unavailable" });
      if (e.status === 401 && e.body.password_required) {
        return setPhase({ kind: "password", error: null, busy: false });
      }
      if (e.status === 401) return setPhase({ kind: "relink" });
      if (e.status === 0 || e.status >= 500)
        return setPhase({ kind: "offline", message: e.message });
    }
    setPhase({
      kind: "offline",
      message: "Something went wrong opening this link.",
    });
  }, []);

  /** Trade the token (or, after a reload on `/p/`, the cookie) for the visitor's link. */
  const begin = useCallback(() => {
    if (broken) return;
    const t = token.current;
    if (t)
      publicApi
        .unlock(t, undefined, lastName.current)
        .then((u) => opened(u, true), failed);
    else publicApi.share().then((u) => opened(u), failed);
  }, [broken, opened, failed]);

  const start = useCallback(() => {
    setPhase({ kind: "loading" });
    begin();
  }, [begin]);

  useEffect(() => {
    begin();
  }, [begin]);

  /** The cookie stopped working mid-visit (12 h cap): unlock again with the token we still hold,
   *  or ask for the link again when a reload already dropped it. */
  const expired = useCallback(() => {
    if (!token.current) setPhase({ kind: "relink" });
    else if (Date.now() - lastUnlock.current < REUNLOCK_MIN_MS)
      setPhase({ kind: "nocookie" });
    else start();
  }, [start]);

  const gone = useCallback(() => setPhase({ kind: "unavailable" }), []);

  /** Ask the cookie which link it holds now. Another tab may have unlocked again (a new CSRF
   *  token) or opened a different link (a different share) since this page did: take what the
   *  server says, so this tab keeps working and never shows one link under another's name. */
  const recheck = useCallback(async () => {
    try {
      const u = await publicApi.share();
      lastName.current = u.share.name;
      setPhase((p) =>
        p.kind === "open" &&
        p.unlocked.csrf === u.csrf &&
        JSON.stringify(p.unlocked.share) === JSON.stringify(u.share)
          ? p
          : { kind: "open", unlocked: u },
      );
    } catch (e) {
      if (e instanceof PublicError && e.status === 404) gone();
      else if (e instanceof PublicError && e.status === 401) expired();
    }
  }, [gone, expired]);

  const submitPassword = async () => {
    const t = token.current;
    if (!t || !password) return;
    setPhase({ kind: "password", error: null, busy: true });
    try {
      opened(await publicApi.unlock(t, password, lastName.current), true);
    } catch (e) {
      if (e instanceof PublicError && e.status === 401) {
        const left = e.body.tries_left;
        setPhase({
          kind: "password",
          busy: false,
          error:
            typeof left === "number"
              ? `Wrong password. ${left} ${left === 1 ? "try" : "tries"} left before a 15-minute pause.`
              : "Wrong password.",
        });
      } else if (e instanceof PublicError && e.status === 429) {
        setPhase({
          kind: "password",
          busy: false,
          error: "Too many wrong passwords. Try again in 15 minutes.",
        });
      } else {
        failed(e);
      }
    }
  };

  const open = phase.kind === "open" ? phase.unlocked : null;
  const now = nowSeconds();

  return (
    <>
      {/* The app's atmosphere behind every phase (#1596); it follows the motion mode by itself. */}
      <Atmosphere />
      <div
        className={styles.page}
        data-testid="share-viewer"
        data-phase={phase.kind}
      >
        <header className={styles.bar}>
          <span className={styles.brand}>
            <BrandMark size={20} />
            BattleLab
          </span>
          {open ? (
            <>
              <span className={styles.title}>
                {open.share.title || "Shared session"}
              </span>
              <span className={styles.facts}>
                <span className={`${styles.chip} ${styles.chipMode}`}>
                  {modeChip(open.share.mode)}
                </span>
                {open.share.expires_at != null && (
                  <span className={styles.chip}>
                    {expiryChip(open.share.expires_at, now)}
                  </span>
                )}
              </span>
            </>
          ) : (
            <span className={styles.sub}>Shared session</span>
          )}
        </header>

        {phase.kind === "loading" && (
          <main className={styles.center}>
            <div className={styles.loading}>
              <Loader kind="radar" label="Opening the shared session…" />
            </div>
          </main>
        )}

        {phase.kind === "password" && (
          <main className={styles.center}>
            <form
              className={styles.card}
              data-testid="share-password-gate"
              onSubmit={(e) => {
                e.preventDefault();
                void submitPassword();
              }}
            >
              <p className={styles.tag}>Password required</p>
              <h1 className={styles.h1}>
                Someone shared a BattleLab session with you
              </h1>
              <p className={styles.muted}>Enter the password they gave you.</p>
              <label className={styles.label} htmlFor="share-pw">
                Password
              </label>
              <div className={styles.pwRow}>
                <input
                  id="share-pw"
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
                {phase.busy ? "Opening…" : "Open session"}
              </button>
            </form>
          </main>
        )}

        {phase.kind === "unavailable" && (
          <main className={styles.center}>
            <div className={styles.notice} data-testid="share-unavailable">
              <p className={styles.tag}>Link unavailable</p>
              <h1 className={styles.h1}>This link no longer works</h1>
              <p className={styles.muted}>
                It may have expired or been revoked, or it was copied
                incompletely. Ask the person who shared it for a new link.
              </p>
            </div>
          </main>
        )}

        {phase.kind === "relink" && (
          <main className={styles.center}>
            <div className={styles.notice} data-testid="share-relink">
              <p className={styles.tag}>Open the link again</p>
              <h1 className={styles.h1}>
                This page needs the link you were given
              </h1>
              <p className={styles.muted}>
                For safety the link isn’t kept in the address bar. Open the full
                link from the message it came in.
              </p>
            </div>
          </main>
        )}

        {phase.kind === "nocookie" && (
          <main className={styles.center}>
            <div className={styles.notice} data-testid="share-nocookie">
              <p className={styles.tag}>Cookies needed</p>
              <h1 className={styles.h1}>
                This browser didn’t keep the link’s cookie
              </h1>
              <p className={styles.muted}>
                A shared session needs one cookie for this site, set when the
                link opens. Allow cookies for this site, then open the link
                again.
              </p>
            </div>
          </main>
        )}

        {phase.kind === "offline" && (
          <main className={styles.center}>
            <div className={styles.notice} data-testid="share-offline">
              <p className={styles.tag}>Can’t open right now</p>
              <h1 className={styles.h1}>{phase.message}</h1>
              <button type="button" className={styles.primary} onClick={start}>
                Try again
              </button>
            </div>
          </main>
        )}

        {open && open.share.kind === "api" && (
          <ApiView
            // One view per link: a different link (another tab re-unlocked) is a fresh mount —
            // no snapshot, cursor or composer state carries over from the old one.
            key={open.share.link}
            unlocked={open}
            onShare={(share) => {
              lastName.current = share.name;
              setPhase({ kind: "open", unlocked: { ...open, share } });
            }}
            onGone={gone}
            onExpired={expired}
            onRecheck={recheck}
          />
        )}

        {open && open.share.kind === "terminal" && (
          <main className={styles.center}>
            <div className={styles.notice} data-testid="share-terminal-pending">
              <p className={styles.tag}>Terminal session</p>
              <h1 className={styles.h1}>
                Shared terminal sessions can’t be shown yet
              </h1>
              <p className={styles.muted}>
                This link is valid, but this BattleLab can’t show a terminal to
                visitors yet. Ask the person who shared it to send a link to an
                API session instead, or try again after they update BattleLab.
              </p>
            </div>
          </main>
        )}
      </div>
    </>
  );
}
