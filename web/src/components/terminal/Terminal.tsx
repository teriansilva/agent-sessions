import { FitAddon } from "@xterm/addon-fit";
import { Terminal as Xterm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useCallback, useEffect, useRef, useState } from "react";
import { TermSocket, type TermStatus } from "../../lib/termSocket";
import { type FreshSession, termWsUrl } from "../../lib/termUrl";
import { attachTouchScroll } from "../../lib/touchScroll";
import { THEMES, xtermTheme } from "../../theme/themes";
import { useTheme } from "../../theme/themeStore";
import { Compose } from "./Compose";
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
}: {
  engine: string;
  id: string;
  fresh?: FreshSession;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const sockRef = useRef<TermSocket | null>(null);
  const termRef = useRef<Xterm | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const { theme } = useTheme();
  const [status, setStatus] = useState<TermStatus>({ kind: "connecting" });
  const [coarse] = useState(() => window.matchMedia?.("(pointer: coarse)")?.matches ?? false);

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
    term.open(host);
    termRef.current = term;
    fitRef.current = fit;

    // Indirection so onStatus (fires async) can call resize logic defined below.
    let onConnected = () => {};
    const sock = new TermSocket((have) => termWsUrl(engine, id, have, fresh), {
      onOutput: (b) => term.write(b),
      onStatus: (s) => {
        setStatus(s);
        if (s.kind === "connected") onConnected();
      },
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
      detachTouch();
      touchLayer?.remove();
      ro.disconnect();
      sock.close();
      sockRef.current = null;
      termRef.current = null;
      fitRef.current = null;
      term.dispose();
    };
    // Primitive deps (not the `fresh` object) so a re-render with an equal value doesn't
    // tear down + relaunch the terminal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engine, id, fresh?.cwd, fresh?.bypass]);

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
      </div>
      {/* Action/compose bar everywhere; expanded on touch, collapsed-to-the-bar on desktop. */}
      <Compose sendInput={sendInput} onCopy={handleCopy} defaultOpen={coarse} />
    </div>
  );
}
