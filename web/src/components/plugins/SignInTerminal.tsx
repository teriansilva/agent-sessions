import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { openAppSocket } from "../../lib/termSocket";
import { useTheme } from "../../theme/themeStore";
import { useTermSize } from "../../theme/termSizeStore";
import { useTermFont } from "../../theme/termFontStore";
import { xtermTheme } from "../../theme/themes";
import s from "./PluginSetup.module.css";

/** No session, persistence, logging or reconnect. Unmount closes this one sign-in attempt. */
export function SignInTerminal({ operation, onClose }: { operation: string; onClose: () => void }) {
  const node = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal | null>(null);
  const refitSoon = useRef<() => void>(() => {});
  const close = useRef(onClose);
  const { theme } = useTheme();
  const { size } = useTermSize();
  const { family } = useTermFont();
  const initial = useRef({ theme, size, family });
  useEffect(() => { close.current = onClose; initial.current = { theme, size, family }; });
  useEffect(() => {
    let socket: WebSocket | null = null;
    let term: Terminal | null = null;
    let observer: ResizeObserver | null = null;
    let fitTimer: ReturnType<typeof setTimeout> | undefined;
    let alive = true;
    // StrictMode's abandoned first effect never consumes the one-use sign-in operation.
    const start = setTimeout(() => {
      if (!alive || !node.current) return;
      term = new Terminal({ fontSize: initial.current.size, fontFamily: initial.current.family,
        theme: xtermTheme(initial.current.theme), scrollback: 200, screenReaderMode: true });
      terminal.current = term;
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(node.current);
      const protocol = location.protocol === "https:" ? "wss:" : "ws:";
      socket = openAppSocket(`${protocol}//${location.host}/ws/plugins/signin/${encodeURIComponent(operation)}`);
      socket.binaryType = "arraybuffer";
      const resize = () => {
        clearTimeout(fitTimer);
        fitTimer = setTimeout(() => {
          if (!alive || !term) return;
          fit.fit();
          if (socket?.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ rows: Math.max(2, Math.min(200, term.rows)), cols: Math.max(10, Math.min(300, term.cols)) }));
          }
        }, 80);
      };
      refitSoon.current = resize;
      socket.onopen = () => { resize(); term?.focus(); };
      socket.onmessage = event => {
        if (alive && event.data instanceof ArrayBuffer) term?.write(new Uint8Array(event.data));
      };
      socket.onclose = () => { if (alive) close.current(); };
      term.onData(data => { if (socket?.readyState === WebSocket.OPEN) socket.send(new TextEncoder().encode(data)); });
      observer = new ResizeObserver(resize);
      observer.observe(node.current);
      resize();
    }, 0);
    return () => {
      alive = false;
      clearTimeout(start); clearTimeout(fitTimer);
      observer?.disconnect(); socket?.close(); term?.dispose();
      terminal.current = null;
      refitSoon.current = () => {};
    };
  }, [operation]);
  useEffect(() => {
    if (!terminal.current) return;
    terminal.current.options.theme = xtermTheme(theme);
    terminal.current.options.fontSize = size;
    terminal.current.options.fontFamily = family;
    refitSoon.current();
  }, [theme, size, family]);
  return <div ref={node} className={s.terminal} aria-label="Temporary vendor sign-in terminal" />;
}
