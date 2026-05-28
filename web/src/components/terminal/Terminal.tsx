import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { Terminal as Xterm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { ArrowDown } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { imageFilesFromData } from "../../lib/clipboardImages";
import { TermSocket, type TermStatus } from "../../lib/termSocket";
import { type FreshSession, termWsUrl } from "../../lib/termUrl";
import { attachTouchScroll } from "../../lib/touchScroll";
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
  const [status, setStatus] = useState<TermStatus>({ kind: "connecting" });
  const [coarse] = useState(() => window.matchMedia?.("(pointer: coarse)")?.matches ?? false);
  // Mobile scroll-to-bottom FAB (#187): shown when the viewport has been scrolled
  // up off the live tail. Updated from xterm's onScroll; the click jumps back.
  const [atBottom, setAtBottom] = useState(true);
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

  // Send raw input to the PTY (used by the mobile action bar / compose).
  const sendInput = useCallback((d: string) => {
    sockRef.current?.send({ t: "i", d });
  }, []);
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

    // Initial look from the active theme; a separate effect re-applies on theme change.
    const t0 = THEMES[theme].terminal;
    const term = new Xterm({
      cursorBlink: true,
      fontSize: t0.fontSize,
      scrollback: 10000,
      fontFamily: t0.fontFamily,
      theme: xtermTheme(theme),
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
    const sock = new TermSocket((have) => termWsUrl(engine, id, have, freshRef.current), {
      onOutput: (b) => term.write(b),
      onStatus: (s) => {
        setStatus(s);
        if (s.kind === "connected") onConnected();
      },
      onId: (sid) => onReconcileIdRef.current?.(sid),
    });
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

    term.onData((d) => sock.send({ t: "i", d }));
    term.onResize(sendResize);

    // Paste an image (screenshot) anywhere over the terminal → forward it to Compose as an
    // attachment pill (opening Compose if it was collapsed) so the user actually sees the
    // file land (#157). Capture-phase + stopPropagation so xterm never sees the (text-less)
    // image paste; plain-text paste carries no image files and falls through to xterm (#135).
    const onHostPaste = (e: ClipboardEvent) => {
      const images = imageFilesFromData(e.clipboardData);
      if (!images.length) return;
      e.preventDefault();
      e.stopPropagation();
      composeRef.current?.attachImages(images);
    };
    host.addEventListener("paste", onHostPaste, true);

    const ro = new ResizeObserver(() => refit());
    ro.observe(host);
    // Mobile: the address bar showing/hiding changes the visual viewport height (dvh)
    // well after first paint — refit so the terminal fills the new height.
    const vv = window.visualViewport;
    const onVV = () => refit();
    vv?.addEventListener("resize", onVV);
    // First fit after layout settles (open() can run before the flex/dvh height is final).
    const raf = requestAnimationFrame(() => refit());

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

    sock.connect();
    return () => {
      cancelAnimationFrame(raf);
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
  }, [engine, id]);

  // Re-theme the live terminal on theme change WITHOUT tearing it down. Colours apply
  // immediately; if the font/size changed, fit() recomputes the grid and xterm's
  // onResize handler (wired above) pushes the new dimensions to the pty.
  useEffect(() => {
    const term = termRef.current;
    if (!term) return;
    const t = THEMES[theme].terminal;
    term.options.theme = xtermTheme(theme);
    term.options.fontFamily = t.fontFamily;
    term.options.fontSize = t.fontSize;
    fitRef.current?.fit();
  }, [theme]);

  const text = statusText(status);
  const scrollToTail = useCallback(() => {
    termRef.current?.scrollToBottom();
    setAtBottom(true);
  }, []);
  return (
    <div className={styles.wrap}>
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
      </div>
      {/* Action/compose bar everywhere; expanded on touch, collapsed-to-the-bar on desktop. */}
      <Compose ref={composeRef} sendInput={sendInput} onCopy={handleCopy} defaultOpen={coarse} />
    </div>
  );
}
