import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { openAppSocket } from "../../lib/termSocket";
import { isPasteShortcut } from "../../lib/termKeys";
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
    const host = node.current;
    // Paste over the terminal (#181): forward text to xterm ourselves. Relying on the
    // event reaching xterm's hidden helper textarea is unreliable when the pointer is
    // over the canvas, and xterm's own Ctrl+V reads the async clipboard. A capture
    // listener on the host catches the native paste wherever it lands in the pane.
    const isMac = /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent || "");
    const onHostPaste = (e: ClipboardEvent) => {
      const text = e.clipboardData?.getData("text/plain");
      if (!text) return;
      e.preventDefault();
      e.stopPropagation();
      terminal.current?.paste(text);
    };
    host?.addEventListener("paste", onHostPaste, true);
    // StrictMode's abandoned first effect never consumes the one-use sign-in operation.
    const start = setTimeout(() => {
      if (!alive || !host) return;
      term = new Terminal({ fontSize: initial.current.size, fontFamily: initial.current.family,
        theme: xtermTheme(initial.current.theme), scrollback: 200, screenReaderMode: true });
      terminal.current = term;
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(host);
      // Never forward the paste shortcut as a raw \x16 to the vendor, which binds
      // Ctrl+V to its own clipboard command; the native paste above handles it.
      term.attachCustomKeyEventHandler(e => isPasteShortcut(e, isMac) ? false : true);
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
      observer.observe(host);
      resize();
    }, 0);
    return () => {
      alive = false;
      clearTimeout(start); clearTimeout(fitTimer);
      observer?.disconnect(); socket?.close(); term?.dispose();
      host?.removeEventListener("paste", onHostPaste, true);
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
