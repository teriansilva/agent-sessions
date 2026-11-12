import { useCallback, useEffect, useState } from "react";

import { BrandMark } from "../components/brand/Brand";
import { Avatar } from "../components/chat/Avatar";
import { Atmosphere } from "../components/hud/Atmosphere";
import { Loader } from "../components/hud/Loader";
import { Markdown } from "../components/structured/Markdown";
import { expiryChip, nowSeconds } from "../lib/shareFormat";
import { followDeviceMotion } from "../theme/motion";
import styles from "../share/ShareViewer.module.css";
import own from "./ReplyViewer.module.css";
import {
  envelopeUrl,
  openEnvelope,
  parseLink,
  WrongKeyError,
  type ReplyContent,
  type ReplyEnvelope,
} from "./seal";

type Phase =
  | { kind: "loading" }
  | { kind: "incomplete" }
  | { kind: "unavailable" }
  | { kind: "offline" }
  | { kind: "password"; env: ReplyEnvelope; key: string; error: string | null; busy: boolean }
  | { kind: "open"; env: ReplyEnvelope; content: ReplyContent };

function when(ts: number | null): string | null {
  if (!ts) return null;
  return new Date(ts * 1000).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function hidden(c: ReplyContent): string | null {
  if (c.tools_hidden <= 0) return null;
  const n = `${c.tools_hidden}${c.tools_more ? "+" : ""}`;
  return `${n} command${c.tools_hidden === 1 && !c.tools_more ? "" : "s"} & tools hidden`;
}

/** A reply link (#1956): ONE agent reply, read only, opened in this page with the key from the
 *  link's fragment. The server only ever held ciphertext; the key and any password stay here.
 *  Nothing else of the session exists on this page — no prompt, no other turn, no tool detail. */
export function ReplyViewer() {
  // The link is read once: without an id and key there is nothing to fetch.
  const [link] = useState(() => parseLink(window.location));
  const [phase, setPhase] = useState<Phase>(() =>
    link.id && link.key ? { kind: "loading" } : { kind: "incomplete" },
  );
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  useEffect(() => followDeviceMotion(), []);

  useEffect(() => {
    const { id, key } = link;
    if (!id || !key) return;
    let alive = true;
    (async () => {
      let env: ReplyEnvelope;
      try {
        const r = await fetch(envelopeUrl(id), {
          cache: "no-store",
          credentials: "omit",
          referrerPolicy: "no-referrer",
        });
        if (r.status === 404) {
          if (alive) setPhase({ kind: "unavailable" });
          return;
        }
        if (!r.ok) throw new Error(String(r.status));
        env = (await r.json()) as ReplyEnvelope;
      } catch {
        if (alive) setPhase({ kind: "offline" });
        return;
      }
      if (env.password) {
        if (alive) setPhase({ kind: "password", env, key, error: null, busy: false });
        return;
      }
      try {
        const content = await openEnvelope(env, key);
        if (alive) setPhase({ kind: "open", env, content });
      } catch {
        if (alive) setPhase({ kind: "incomplete" });
      }
    })();
    return () => {
      alive = false;
    };
  }, [link]);

  const submit = useCallback(async () => {
    if (phase.kind !== "password") return;
    setPhase({ ...phase, busy: true, error: null });
    try {
      const content = await openEnvelope(phase.env, phase.key, password);
      setPassword("");
      setPhase({ kind: "open", env: phase.env, content });
    } catch (e) {
      setPhase({
        ...phase,
        busy: false,
        error: e instanceof WrongKeyError ? "Wrong password." : "This link can’t be opened.",
      });
    }
  }, [phase, password]);

  const open = phase.kind === "open" ? phase : null;
  const expires = phase.kind === "open" || phase.kind === "password" ? phase.env.expires_at : null;

  return (
    <>
      <Atmosphere />
      <div className={styles.page} data-testid="reply-viewer" data-phase={phase.kind}>
        <header className={styles.bar}>
          <span className={styles.brand}>
            <BrandMark size={20} />
            BattleLab
          </span>
          <span className={styles.title}>Shared reply</span>
          <span className={styles.facts}>
            <span className={`${styles.chip} ${styles.chipMode}`}>READ ONLY</span>
            {expires != null && <span className={styles.chip}>{expiryChip(expires, nowSeconds())}</span>}
          </span>
        </header>

        {phase.kind === "loading" && (
          <main className={styles.center}>
            <div className={styles.loading}>
              <Loader kind="radar" label="Opening the shared reply…" />
            </div>
          </main>
        )}

        {phase.kind === "password" && (
          <main className={styles.center}>
            <form
              className={styles.card}
              data-testid="reply-password-gate"
              onSubmit={(e) => {
                e.preventDefault();
                void submit();
              }}
            >
              <p className={styles.tag}>Password required</p>
              <h1 className={styles.h1}>Someone shared a reply with you</h1>
              <p className={styles.muted}>
                Enter the password they gave you. It is checked in this page and never sent anywhere.
              </p>
              <label className={styles.label} htmlFor="reply-pw">
                Password
              </label>
              <div className={styles.pwRow}>
                <input
                  id="reply-pw"
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
              <button type="submit" className={styles.primary} disabled={phase.busy || !password}>
                {phase.busy ? "Opening…" : "Open reply"}
              </button>
            </form>
          </main>
        )}

        {phase.kind === "unavailable" && (
          <main className={styles.center}>
            <div className={styles.notice} data-testid="reply-unavailable">
              <p className={styles.tag}>Link unavailable</p>
              <h1 className={styles.h1}>This link no longer works</h1>
              <p className={styles.muted}>
                It may have expired or been revoked. Ask the person who shared it for a new link.
              </p>
            </div>
          </main>
        )}

        {phase.kind === "incomplete" && (
          <main className={styles.center}>
            <div className={styles.notice} data-testid="reply-incomplete">
              <p className={styles.tag}>Incomplete link</p>
              <h1 className={styles.h1}>This link is missing its key</h1>
              <p className={styles.muted}>
                The part after <code>#</code> opens the reply. Copy the whole link from the message
                it came in.
              </p>
            </div>
          </main>
        )}

        {phase.kind === "offline" && (
          <main className={styles.center}>
            <div className={styles.notice} data-testid="reply-offline">
              <p className={styles.tag}>Can’t open right now</p>
              <h1 className={styles.h1}>This BattleLab can’t be reached</h1>
              <p className={styles.muted}>Try again in a moment.</p>
            </div>
          </main>
        )}

        {open && (
          <main className={own.main} data-testid="reply-open">
            <article className={own.reply} aria-label={`Reply from ${open.content.agent}`}>
              <p className={own.who}>
                <Avatar spec={{ kind: "agent" }} className={own.avatar} />
                <span>{open.content.agent}</span>
                {when(open.content.ts) && <time className={own.ts}>{when(open.content.ts)}</time>}
              </p>
              {hidden(open.content) && (
                <p className={own.hidden} data-testid="reply-tools-hidden">
                  {hidden(open.content)}
                </p>
              )}
              <div className={own.body}>
                <Markdown text={open.content.reply} />
              </div>
            </article>
            <p className={own.foot}>Shared from a BattleLab session. Only this reply is visible.</p>
          </main>
        )}
      </div>
    </>
  );
}
