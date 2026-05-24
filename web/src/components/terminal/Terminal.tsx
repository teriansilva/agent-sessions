import { FitAddon } from "@xterm/addon-fit";
import { Terminal as Xterm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useRef, useState } from "react";
import { TermSocket, type TermStatus } from "../../lib/termSocket";
import { type FreshSession, termWsUrl } from "../../lib/termUrl";
import { attachTouchScroll } from "../../lib/touchScroll";
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

    const sock = new TermSocket((have) => termWsUrl(engine, id, have, fresh), {
      onOutput: (b) => term.write(b),
      onStatus: setStatus,
    });

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
    // touch for selection), so phones felt stuck. attachTouchScroll translates a drag
    // into line scrolls (capture phase, non-passive) — see lib/touchScroll.
    const detachTouch = attachTouchScroll(host, term);

    sock.connect();
    return () => {
      detachTouch();
      ro.disconnect();
      sock.close();
      term.dispose();
    };
    // Primitive deps (not the `fresh` object) so a re-render with an equal value doesn't
    // tear down + relaunch the terminal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engine, id, fresh?.cwd, fresh?.bypass]);

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
