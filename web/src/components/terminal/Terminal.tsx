import { FitAddon } from "@xterm/addon-fit";
import { Terminal as Xterm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useRef, useState } from "react";
import { TermSocket, type TermStatus } from "../../lib/termSocket";
import { dragToLines, type ScrollAccum } from "../../lib/touchScroll";
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
export function Terminal({ engine, id }: { engine: string; id: string }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<TermStatus>({ kind: "connecting" });

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const term = new Xterm({
      cursorBlink: true,
      fontSize: 13,
      scrollback: 10000,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      theme: { background: "#0e0e0e" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    fit.fit();

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const key = `${encodeURIComponent(engine)}:${encodeURIComponent(id)}`;
    const sock = new TermSocket(
      (have) => `${proto}://${location.host}/ws/term/${key}?have=${have}`,
      { onOutput: (b) => term.write(b), onStatus: setStatus },
    );

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

    term.onData((d) => sock.send({ t: "i", d }));
    term.onResize(sendResize);
    const ro = new ResizeObserver(() => {
      fit.fit();
      sendResize();
    });
    ro.observe(host);

    // Touch scroll: xterm doesn't scroll its scrollback on a one-finger drag (it grabs
    // touch for selection), so phones felt stuck. Translate a single-finger drag into
    // line scrolls ourselves; a tap (no move) is left alone so it still focuses + opens
    // the keyboard. preventDefault stops the page from rubber-banding under the drag.
    const acc: ScrollAccum = { remainder: 0 };
    let lastY = 0;
    let dragging = false;
    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length !== 1) return;
      lastY = e.touches[0].clientY;
      acc.remainder = 0;
      dragging = true;
    };
    const onTouchMove = (e: TouchEvent) => {
      if (!dragging || e.touches.length !== 1) return;
      const y = e.touches[0].clientY;
      const dy = lastY - y; // finger up (dy>0) → scroll toward newer output
      lastY = y;
      const lines = dragToLines(dy, host.clientHeight / (term.rows || 24), acc);
      if (lines !== 0) term.scrollLines(lines);
      e.preventDefault();
    };
    const endTouch = () => {
      dragging = false;
    };
    host.addEventListener("touchstart", onTouchStart, { passive: true });
    host.addEventListener("touchmove", onTouchMove, { passive: false });
    host.addEventListener("touchend", endTouch, { passive: true });
    host.addEventListener("touchcancel", endTouch, { passive: true });

    sock.connect();
    return () => {
      host.removeEventListener("touchstart", onTouchStart);
      host.removeEventListener("touchmove", onTouchMove);
      host.removeEventListener("touchend", endTouch);
      host.removeEventListener("touchcancel", endTouch);
      ro.disconnect();
      sock.close();
      term.dispose();
    };
  }, [engine, id]);

  const text = statusText(status);
  return (
    <div className={styles.wrap}>
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
  );
}
