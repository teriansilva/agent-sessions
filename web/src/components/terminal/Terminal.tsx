import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { Terminal as Xterm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { ArrowDown } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../../lib/api";
import { getBrowserFp, getTabId } from "../../lib/browserFp";
import { getDeviceLabel } from "../../lib/deviceLabel";
import { imageFilesFromData } from "../../lib/clipboardImages";
import { isPasteShortcut } from "../../lib/termKeys";
import { useConfig } from "../../app/config";
import { useSessionsStore } from "../../app/sessionsStore";
import {
  TermSocket,
  type TermGateHolder,
  type TermRole,
  type TermStatus,
} from "../../lib/termSocket";
import { type FreshSession, termWsUrl } from "../../lib/termUrl";
import { GateOverlay } from "./GateOverlay";
import { attachTouchScroll } from "../../lib/touchScroll";
import { useAccent } from "../../theme/accentStore";
import { THEMES, xtermTheme } from "../../theme/themes";
import { useTheme } from "../../theme/themeStore";
import { Compose, type ComposeHandle } from "./Compose";
import styles from "./Terminal.module.css";

function statusText(s: TermStatus): string {
  switch (s.kind) {
    case "connecting":
      return "connecting…";
    case "reconnecting":
      return "reconnecting…";
    case "rejected":
      return s.reason;
    case "connected":
      return "";
  }
}

/** Persistent HUD panel-header readout (#211 4c): a mono STATUS tag + LED class for the
 *  reusable .hud-led primitive. Distinct from statusText (the transient corner overlay) — this
 *  one is always present so the terminal panel always declares its link state. */
function headStatus(s: TermStatus): { label: string; led: string } {
  switch (s.kind) {
    case "connected":
      return { label: "LIVE", led: "up" };
    case "connecting":
      return { label: "CONNECTING", led: "" };
    case "reconnecting":
      return { label: "RECONNECTING", led: "" };
    case "rejected":
      return { label: "OFFLINE", led: "down" };
  }
}

/** The live terminal: an xterm pane bridged to /ws/term/{engine}:{id} via TermSocket.
 *  Output is written verbatim; keystrokes and resize go back as JSON; reconnect +
 *  delta-resume (never-blank) is owned by TermSocket. Remount per session via `key`. */
export function Terminal({
  engine,
  id,
  fresh,
  onReconcileId,
}: {
  engine: string;
  id: string;
  fresh?: FreshSession;
  /** Server reconciled to the real engine-qualified id (#127, opencode new-session).
   *  The owner converges the URL/sidebar without tearing down the socket. */
  onReconcileId?: (sid: string) => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const sockRef = useRef<TermSocket | null>(null);
  const composeRef = useRef<ComposeHandle>(null);
  const termRef = useRef<Xterm | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const { theme } = useTheme();
  const { accent } = useAccent();
  // Resolve the human session title for the panel header (#232) from the shared store the
  // sidebar fills (matched by engine+uuid). Falls back to a short id before the
  // list has loaded / for a fresh placeholder session. Read-only: it never re-keys the socket.
  const { sessions } = useSessionsStore();
  const [status, setStatus] = useState<TermStatus>({ kind: "connecting" });
  const [coarse] = useState(() => window.matchMedia?.("(pointer: coarse)")?.matches ?? false);
  // Compose default state (#254): the per-user pref overrides the device heuristic. "auto"
  // (and an unloaded config) keeps the heuristic — expanded on touch, collapsed on desktop.
  const composeMode = useConfig()?.compose_default ?? "auto";
  const composeDefaultOpen =
    composeMode === "open" ? true : composeMode === "collapsed" ? false : coarse;
  // Mobile scroll-to-bottom FAB (#187): shown when the viewport has been scrolled
  // up off the live tail. Updated from xterm's onScroll; the click jumps back.
  const [atBottom, setAtBottom] = useState(true);
  // Per-tab ownership (#184 slice 3): the server's verdict on whether this WS
  // bridge holds the owner role or is a read-only secondary. Default is "owner"
  // until the server says otherwise — backward-compatible with the pre-slice-3
  // server which never sends a role frame at all.
  const [role, setRole] = useState<TermRole>("owner");
  // Single-active-viewer gate (#293, flag on): non-null when this device is NOT the active
  // viewer (someone else holds the session, or we were just taken over). `gateMode`
  // distinguishes "opened while active elsewhere" from "got taken over mid-session".
  const [gate, setGate] = useState<TermGateHolder | null>(null);
  const [gateMode, setGateMode] = useState<"busy" | "taken">("busy");
  // True once the server confirmed us as owner (a {t:"role","role":"owner"} frame). Lets a
  // subsequent gate frame tell "taken over" (we WERE owner) from "in use" (we never were).
  const confirmedOwnerRef = useRef(false);
  const navigate = useNavigate();
  // Bumped to tear down + reopen the socket. `takeoverEpoch` is the Take-over path (#184) and
  // `reconnectEpoch` is a plain reattach (e.g. after a #331 restart). Whether the fresh connect
  // demands ?force=1 is decided ONLY by `forceNextConnectRef` (set by takeover, consumed by the
  // effect) — NOT by which epoch moved — so a restart reattach never silently force-takes-over a
  // session another tab may have claimed during the restart window (Hermes #332).
  const [takeoverEpoch, setTakeoverEpoch] = useState(0);
  const [reconnectEpoch, setReconnectEpoch] = useState(0);
  const forceNextConnectRef = useRef(false);
  // Keep the latest reconcile callback in a ref so the {t:"id"} handler always calls the
  // current one WITHOUT the socket effect depending on it (a changing callback identity
  // must never tear down + relaunch the live terminal). Updated in an effect (writing a
  // ref during render is disallowed by react-hooks).
  const onReconcileIdRef = useRef(onReconcileId);
  useEffect(() => {
    onReconcileIdRef.current = onReconcileId;
  }, [onReconcileId]);

  // Freeze the fresh-launch params for the lifetime of this terminal instance (its `key`).
  // The owner DROPS route state during placeholder→real convergence (#127, opencode); if the
  // socket effect depended on `fresh`, that drop would tear down the live socket and reconnect
  // via termWsUrl(..., undefined) — omitting new=1 while the id is still the pending
  // placeholder, which the server rejects as a plain attach (4404), killing the very terminal
  // the converge is meant to preserve. useRef captures only the first render's value, so later
  // prop changes can't move it; a genuine session switch remounts via `key` and re-seeds it.
  const freshRef = useRef(fresh);

  // Send raw input to the PTY (used by the mobile action bar / compose). Returns whether the frame
  // was actually delivered (socket OPEN) — Compose uses this so it never submits a bare Enter after
  // a clear/paste that got dropped mid-reconnect (the empty-compose bug #287).
  const sendInput = useCallback((d: string) => sockRef.current?.send({ t: "i", d }) ?? false, []);
  // Current socket id (bumped each reconnect) so Compose can detect a reconnect between its frames.
  const connEpoch = useCallback(() => sockRef.current?.connectionId ?? -1, []);
  // Copy the current selection, or the whole buffer if nothing is selected.
  const handleCopy = useCallback(() => {
    const t = termRef.current;
    if (!t) return;
    let sel = t.getSelection();
    if (!sel) {
      t.selectAll();
      sel = t.getSelection();
      t.clearSelection();
    }
    if (sel) void navigator.clipboard?.writeText(sel);
  }, []);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    // Initial look from the active theme; a separate effect re-applies on theme/accent change.
    // The cursor follows the brand accent (#211 Phase 2), overriding the theme's default.
    const t0 = THEMES[theme].terminal;
    const term = new Xterm({
      cursorBlink: true,
      fontSize: t0.fontSize,
      // Lines of live (dtach-stream) scroll-up the browser retains while connected — distinct
      // from the rendered transcript. 50k keeps a deep session in reach; xterm stores lines
      // compactly so the memory cost is modest. Pairs with the server ring (_MAX_BUF) that backs
      // reconnect replay.
      scrollback: 50000,
      fontFamily: t0.fontFamily,
      theme: { ...xtermTheme(theme), cursor: accent },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    // Make URLs in agent output clickable (#158). Opens in a new tab, deliberately denying
    // window.opener access (so the linked page can't navigate this tab) + the Referer header
    // (privacy + don't leak which agent-sessions session we came from).
    term.loadAddon(
      new WebLinksAddon((_e, uri) => {
        window.open(uri, "_blank", "noopener,noreferrer");
      }),
    );
    term.open(host);
    termRef.current = term;
    fitRef.current = fit;

    // Don't forward the paste shortcut (Ctrl+V / Cmd+V) to the PTY as a raw keystroke
    // (#209): the agent (Claude Code) binds Ctrl+V to "paste image from clipboard" and
    // reads the SERVER clipboard, printing "no image found in clipboard" on a text paste.
    // Returning false makes xterm skip the key WITHOUT preventDefault, so the browser's
    // native paste still fires → onHostPaste → term.paste(text), one clean paste.
    const isMac = /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent || "");
    term.attachCustomKeyEventHandler((e) => !isPasteShortcut(e, isMac));

    // #187: track whether the viewport is sitting at the live tail. xterm fires
    // onScroll with the topmost line of the viewport whenever the user scrolls or
    // new output pushes the buffer; "at bottom" means viewportY has caught up to
    // baseY (the bottom of the scrollback). Eight-line dead zone so a single
    // wheel click while live output is streaming doesn't flicker the FAB on/off.
    const SCROLL_DEAD_ZONE = 8;
    const computeAtBottom = () => {
      const buf = term.buffer?.active;
      if (!buf) return true;
      return buf.baseY - buf.viewportY <= SCROLL_DEAD_ZONE;
    };
    const updateAtBottom = () => setAtBottom(computeAtBottom());
    term.onScroll?.(updateAtBottom);

    // Indirection so onStatus (fires async) can call resize logic defined below.
    let onConnected = () => {};
    // Per-tab ownership (#184): include fp + tab + a one-shot force flag in the
    // URL. The force flag is consumed by the FIRST connect of this terminal
    // instance — a reconnect after a transient drop must NOT keep demanding
    // takeover (the server would shut out a legitimate prior owner).
    const fp = getBrowserFp();
    const tabId = getTabId();
    // Force is armed ONLY by the Take-over button (forceNextConnectRef), and consumed here so it
    // applies to exactly this effect's fresh connect — a restart/reconnect epoch bump leaves it
    // false → a plain attach, never a silent takeover (#332).
    const wantsForce = forceNextConnectRef.current;
    forceNextConnectRef.current = false;
    let forceConsumed = false;
    // new=1 (launch) is a one-shot too: the FIRST connect launches the session; a reconnect must
    // ATTACH the now-existing session, not relaunch it. Re-sending new=1 makes the server run
    // `claude --session-id <id>` again → claude rejects the existing id ("session already in use")
    // → EOF → reconnect loop. EXCEPTION: an opencode placeholder (`new-<uuid>`) keeps new=1 until it
    // converges to its real id (#127) — the session doesn't exist under a real id yet.
    let freshConsumed = false;
    const sock = new TermSocket(
      (have) => {
        const f = wantsForce && !forceConsumed;
        forceConsumed = true;
        const keepFresh = !freshConsumed || id.startsWith("new-");
        const fresh = keepFresh ? freshRef.current : undefined;
        // Pass our current grid so the server sizes the pty to us from the start (#227) — a
        // launched agent then renders at the right width instead of 80x24→reflow. cols/rows
        // are populated by the pre-connect fit below (and stay current across reconnects).
        return termWsUrl(engine, id, have, fresh, {
          fp,
          tabId,
          force: f,
          cols: term.cols,
          rows: term.rows,
          label: getDeviceLabel(),
        });
      },
      {
        onOutput: (b) => term.write(b),
        onStatus: (s) => {
          setStatus(s);
          if (s.kind === "connected") {
            // The socket OPENED → the server received new=1 and launched. Only NOW stop sending the
            // launch params: if a first attempt is closed (watchdog / transient drop) BEFORE it
            // opens, the server never saw the launch, so the retry must relaunch — not attach to a
            // not-yet-existent session. (opencode `new-` placeholders keep new=1 until converged.)
            freshConsumed = true;
            onConnected();
          }
        },
        onId: (sid) => onReconcileIdRef.current?.(sid),
        onRole: (r) => {
          setRole(r);
          // We're the active viewer again → clear any gate and remember we held it (so a
          // later demotion gate reads as "taken over", not "in use").
          if (r === "owner") {
            confirmedOwnerRef.current = true;
            setGate(null);
          }
        },
        // {t:"gate"} (#293): we're NOT the active viewer. Show the gate; "taken" if we WERE
        // owner (demoted mid-session), else "busy" (opened while active elsewhere). Reset the
        // owner flag — a take-over (force reconnect) re-confirms it via a fresh role frame.
        onGate: (holder) => {
          setGateMode(confirmedOwnerRef.current ? "taken" : "busy");
          confirmedOwnerRef.current = false;
          setGate(holder ?? { label: "" });
        },
      },
    );
    sockRef.current = sock;

    // Only push a resize when the grid actually changed — a bare scrollbar toggle
    // would otherwise SIGWINCH the agent into a full repaint (visible flicker loop).
    let lastCols = 0;
    let lastRows = 0;
    const sendResize = () => {
      if (term.cols === lastCols && term.rows === lastRows) return;
      lastCols = term.cols;
      lastRows = term.rows;
      sock.send({ t: "r", cols: term.cols, rows: term.rows });
    };
    // Refit to the container, then push the size. Used on mount, on container/visual-
    // viewport resize, and on every (re)connect — a fresh dtach pty defaults to 80x24,
    // so we MUST tell it our real size or the agent renders at the wrong dimensions
    // (garbled / blank-until-scroll until something else triggers a resize).
    const refit = (force = false) => {
      fit.fit();
      if (force) lastCols = lastRows = 0; // bypass the dedupe so the new pty is sized
      sendResize();
    };
    onConnected = () => refit(true);
    // Coalesce resize bursts (#227): mobile's address-bar show/hide fires a stream of
    // visualViewport / ResizeObserver events. Refitting on each one SIGWINCHes the agent into a
    // full repaint per event, and a repaint-heavy TUI (e.g. Claude Code) piles those frames into
    // scrollback as duplicated/garbled content. Debounce so a burst settles into ONE refit.
    // (Connect + first-paint stay immediate via refit() — a fresh pty must be sized at once.)
    let resizeTimer: number | undefined;
    const refitSoon = () => {
      if (resizeTimer != null) clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        resizeTimer = undefined;
        refit();
      }, 120);
    };

    term.onData((d) => sock.send({ t: "i", d }));
    term.onResize(sendResize);

    // Paste over the terminal (#157 + #181):
    // - Image paste → forward to Compose as an attachment pill (opens Compose if
    //   it was collapsed); the image never reaches the PTY.
    // - Text paste → forward to xterm via ``term.paste(text)``. Without this the
    //   capture-phase listener has to rely on the paste event reaching xterm's
    //   hidden helper textarea, which doesn't happen reliably when the cursor
    //   is over the canvas rather than the textarea — the user saw a paste that
    //   did nothing and had to right-click → Paste instead (#181). ``term.paste``
    //   respects bracketed-paste mode and matches what xterm's own textarea
    //   handler would do, so the agent sees one clean paste.
    const onHostPaste = (e: ClipboardEvent) => {
      const images = imageFilesFromData(e.clipboardData);
      if (images.length) {
        e.preventDefault();
        e.stopPropagation();
        composeRef.current?.attachImages(images);
        return;
      }
      const text = e.clipboardData?.getData("text/plain");
      if (text) {
        e.preventDefault();
        e.stopPropagation();
        termRef.current?.paste(text);
      }
    };
    host.addEventListener("paste", onHostPaste, true);

    const ro = new ResizeObserver(() => refitSoon());
    ro.observe(host);
    // Mobile: the address bar showing/hiding changes the visual viewport height (dvh)
    // well after first paint — refit (debounced) so the terminal fills the new height
    // without a per-event SIGWINCH storm (#227).
    const vv = window.visualViewport;
    const onVV = () => refitSoon();
    vv?.addEventListener("resize", onVV);
    // Connect only once the measured grid has gone QUIET. On a fast in-app session switch the panel
    // is still settling when the terminal mounts — the mobile sidebar-drawer close animation, an
    // address-bar / visualViewport correction (observed rows 61→66) — so the grid keeps changing for
    // several frames AFTER the first couple agree. Attaching at that un-settled size, then correcting
    // it, SIGWINCHes the agent into a clear+repaint that WIPES the just-delivered scroll-up — the
    // "switch almost always needs F5" race. A reload doesn't hit it because a fresh page load measures
    // the settled size once.
    //
    // So don't trust a momentary match: require the grid to hold UNCHANGED for a quiet window, and
    // RESET that window on any change. A bounded settle (a drawer animation is continuous frame-to-
    // frame change) therefore keeps resetting the counter until it ends, and we attach at the final
    // grid with no correcting resize. Frame-counted (not wall-clock) so it's deterministic under test;
    // capped so a never-quiet layout still connects.
    const QUIET_FRAMES = 8; // grid must hold steady this many frames (~130ms) before we trust it
    const MAX_FRAMES = 90; //  ~1.5s hard cap so a perpetually-jittering layout still attaches
    let settleRaf = 0;
    let lastC = -1;
    let lastR = -1;
    let quietFrames = 0;
    let totalFrames = 0;
    const connectWhenStable = () => {
      if (sock !== sockRef.current) return; // superseded by a remount
      try {
        fit.fit();
      } catch {
        /* host not measurable yet → try again next frame */
      }
      const c = term.cols;
      const r = term.rows;
      if (c > 1 && r > 1 && c === lastC && r === lastR) {
        quietFrames++;
      } else {
        lastC = c;
        lastR = r;
        quietFrames = 0; // the grid moved → restart the quiet window (waits out the settle)
      }
      if (quietFrames >= QUIET_FRAMES || totalFrames++ >= MAX_FRAMES) {
        sock.connect(); // grid quiet → settled → attach at the final size, no correcting resize
      } else {
        settleRaf = requestAnimationFrame(connectWhenStable);
      }
    };

    // Touch scroll: on coarse-pointer devices lay a transparent capture surface over the
    // terminal area — claiming the touch there (xterm never sees it) is the only thing
    // that scrolls reliably; its text layer otherwise hijacks the drag. Quick drag
    // scrolls (+ momentum); a tap (re)opens the keyboard. See lib/touchScroll.
    let touchLayer: HTMLDivElement | undefined;
    if (coarse && host.parentElement) {
      touchLayer = document.createElement("div");
      touchLayer.className = styles.touchLayer;
      touchLayer.dataset.touchSurface = ""; // e2e hook
      host.parentElement.appendChild(touchLayer); // host.parentElement = .termArea
    }
    const detachTouch = attachTouchScroll(touchLayer ?? host, term);

    // Attach once the grid is stable (see connectWhenStable) — NOT synchronously, or a still-
    // settling panel makes the post-connect resize wipe the transcript scroll-up (the race).
    connectWhenStable();
    return () => {
      cancelAnimationFrame(settleRaf);
      if (resizeTimer != null) clearTimeout(resizeTimer);
      vv?.removeEventListener("resize", onVV);
      host.removeEventListener("paste", onHostPaste, true);
      detachTouch();
      touchLayer?.remove();
      ro.disconnect();
      sock.close();
      sockRef.current = null;
      termRef.current = null;
      fitRef.current = null;
      term.dispose();
    };
    // Identity-only deps: this socket lives and dies with the terminal's `key` (engine:id).
    // `fresh` is intentionally excluded — it's read once via freshRef so self-convergence
    // (which clears route state) can't tear down + relaunch the live terminal. See freshRef.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engine, id, takeoverEpoch, reconnectEpoch]);

  // Re-theme the live terminal on theme/accent change WITHOUT tearing it down. Colours apply
  // immediately; if the font/size changed, fit() recomputes the grid and xterm's
  // onResize handler (wired above) pushes the new dimensions to the pty. The cursor tracks
  // the brand accent (#211 Phase 2).
  useEffect(() => {
    const term = termRef.current;
    if (!term) return;
    const t = THEMES[theme].terminal;
    term.options.theme = { ...xtermTheme(theme), cursor: accent };
    term.options.fontFamily = t.fontFamily;
    term.options.fontSize = t.fontSize;
    fitRef.current?.fit();
  }, [theme, accent]);

  const text = statusText(status);
  const head = headStatus(status);
  const row = sessions.find((s) => s.engine === engine && s.uuid === id);
  const title = row?.title || row?.first_user_message || `${id.slice(0, 8)}…`;
  const scrollToTail = useCallback(() => {
    termRef.current?.scrollToBottom();
    setAtBottom(true);
  }, []);
  const takeover = useCallback(() => {
    // Arm the one-shot force flag, then bump the epoch: the effect reconnects and the fresh
    // connect carries ?force=1, demoting the prior owner on the server (#184).
    forceNextConnectRef.current = true;
    setTakeoverEpoch((n) => n + 1);
  }, []);
  // Manual session restart (#331): recover a WEDGED session (agent alive but no longer painting).
  // POST kills the live master; the next attach finds no master and resumes from disk — so once it
  // returns we bump the reconnect epoch to reattach (which relaunches via the engine's resume argv).
  // The conversation is preserved on disk. A different active viewer 409s; offer a forced retry.
  const [restarting, setRestarting] = useState(false);
  const restart = useCallback(async () => {
    if (restarting) return;
    if (
      !window.confirm(
        "Restart this session? The agent process is killed and the conversation is resumed " +
          "from disk. Use this when the terminal is stuck/blank and won't respond.",
      )
    )
      return;
    const sid = `${engine}:${id}`;
    const opts = { fp: getBrowserFp(), tabId: getTabId() };
    setRestarting(true);
    try {
      try {
        await api.restart(sid, opts);
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          if (!window.confirm("Another viewer is active on this session. Restart anyway?")) return;
          await api.restart(sid, { ...opts, force: true });
        } else {
          throw e;
        }
      }
      // Reattach (NOT a takeover): the master is gone, so a plain fresh connect relaunches +
      // resumes. forceNextConnectRef stays false → no ?force=1, so if another tab claimed the
      // session during the restart window we don't silently demote it (Hermes #332).
      setReconnectEpoch((n) => n + 1);
    } catch {
      // Best-effort: leave the terminal as-is so the user can retry (a transient failure shows no
      // change rather than a broken state).
    } finally {
      setRestarting(false);
    }
  }, [engine, id, restarting]);
  return (
    <div className={styles.wrap}>
      {/* Panel header (#211 4c): mono channel id + a persistent STATUS // LIVE readout with a
          semantic LED, so the terminal panel always declares which agent + link state it is. */}
      <div className={styles.panelHead}>
        <span className={`hud-tag ${styles.headLeft}`}>
          <span className={styles.headEng}>{engine.toUpperCase()} //</span>
          <b className={styles.headTitle} title={title}>
            {title}
          </b>
        </span>
        <span className="hud-tag">
          <span className={`hud-led ${head.led}`} aria-hidden="true" />
          STATUS // <b className="num">{head.label}</b>
        </span>
        <button
          type="button"
          className={styles.restartBtn}
          onClick={restart}
          disabled={restarting}
          title="Restart this session: kill the agent process and resume from disk (recovers a stuck/blank terminal)"
          aria-label="Restart session"
        >
          {restarting ? "RESTARTING…" : "RESTART"}
        </button>
      </div>
      <div className={styles.termArea}>
        {text && (
          <div
            className={`${styles.status} ${status.kind === "rejected" ? styles.rejected : ""}`}
            role="status"
          >
            {text}
          </div>
        )}
        <div ref={hostRef} className={styles.term} />
        {coarse && !atBottom && (
          <button
            type="button"
            className={styles.scrollFab}
            aria-label="Scroll to bottom"
            title="Scroll to bottom"
            onClick={scrollToTail}
          >
            <ArrowDown size={20} />
          </button>
        )}
        {role === "secondary" && (
          <div className={styles.secondaryBanner} role="status">
            <span>This session is open in another tab. You're viewing in read-only mode.</span>
            <button
              type="button"
              className={styles.takeoverBtn}
              onClick={takeover}
              aria-label="Take over this session"
            >
              Take over
            </button>
          </div>
        )}
        {/* Single-active-viewer gate (#293, flag on): full take-over page over the blurred
            console. Take over = force-reconnect (promotes this device); Cancel = new session. */}
        {gate && (
          <GateOverlay
            holder={gate}
            mode={gateMode}
            onTakeover={takeover}
            onCancel={() => navigate("/")}
          />
        )}
      </div>
      {/* Action/compose bar everywhere; default state per the compose pref (#254), falling back
          to the device heuristic — expanded on touch, collapsed-to-the-bar on desktop. */}
      <Compose
        ref={composeRef}
        sendInput={sendInput}
        connEpoch={connEpoch}
        onCopy={handleCopy}
        defaultOpen={composeDefaultOpen}
      />
    </div>
  );
}
