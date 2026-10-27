import { type FormEvent, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { openAppSocket } from "../../lib/termSocket";
import { isPasteShortcut } from "../../lib/termKeys";
import { useTheme } from "../../theme/themeStore";
import { useTermSize } from "../../theme/termSizeStore";
import { useTermFont } from "../../theme/termFontStore";
import { useTermScheme } from "../../theme/termSchemeStore";
import { xtermThemeFor } from "../../theme/termSchemes";
import { lastUrl, safeSignInUrl } from "./signInLinks";
import button from "../ui/actionButton.module.css";
import s from "./PluginSetup.module.css";

/** No session, persistence, logging or reconnect. Unmount closes this one sign-in attempt.
 *
 *  When the vendor's command exits, the terminal STAYS with its last screen (`onEnded` lets the
 *  owner re-read the operation): the vendor's own reason for a failed sign-in ("Login failed: …")
 *  is the only diagnosis there is, and BattleLab keeps no copy of it. The last http(s) link the
 *  vendor printed is offered as a button, and a one-line field types a pasted code into the
 *  terminal: a 400-character OAuth URL wraps across five rows and is easy to break by hand. */
export function SignInTerminal({ operation, onEnded }: { operation: string; onEnded: () => void }) {
  const node = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal | null>(null);
  const sendLine = useRef<(text: string) => boolean>(() => false);
  const refitSoon = useRef<() => void>(() => {});
  const close = useRef(onEnded);
  const [link, setLink] = useState<string | null>(null);
  const [ended, setEnded] = useState(false);
  const [line, setLine] = useState("");
  const { theme } = useTheme();
  const { scheme } = useTermScheme();
  const { size } = useTermSize();
  const { family } = useTermFont();
  const initial = useRef({ theme, scheme, size, family });
  useEffect(() => { close.current = onEnded; initial.current = { theme, scheme, size, family }; });
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
      const open = (uri: string) => {
        const safe = safeSignInUrl(uri);
        if (safe) window.open(safe, "_blank", "noopener,noreferrer");
      };
      term = new Terminal({ fontSize: initial.current.size, fontFamily: initial.current.family,
        theme: xtermThemeFor(initial.current.scheme, initial.current.theme), scrollback: 200, screenReaderMode: true,
        // OSC 8 hyperlinks (Claude prints its sign-in URL as one): open directly, without xterm's
        // default confirm() prompt; only http(s) ever opens.
        linkHandler: { activate: (_e, uri) => open(uri), allowNonHttpProtocols: false } });
      terminal.current = term;
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.loadAddon(new WebLinksAddon((_e, uri) => open(uri)));
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
      let scanTimer: ReturnType<typeof setTimeout> | undefined;
      const scan = () => {
        clearTimeout(scanTimer);
        scanTimer = setTimeout(() => {
          if (alive && term) setLink(lastUrl(term.buffer.active));
        }, 100);
      };
      socket.onmessage = event => {
        if (alive && event.data instanceof ArrayBuffer) term?.write(new Uint8Array(event.data), scan);
      };
      socket.onclose = () => {
        if (!alive) return;
        // Keep the last screen: the vendor's exit message is the only record of why it ended.
        term?.write("\r\n\x1b[2m— sign-in ended —\x1b[0m\r\n");
        setEnded(true);
        close.current();
      };
      const send = (data: string) => {
        if (socket?.readyState !== WebSocket.OPEN) return false;
        socket.send(new TextEncoder().encode(data));
        return true;
      };
      sendLine.current = text => send(text + "\r");
      term.onData(data => { send(data); });
      observer = new ResizeObserver(resize);
      observer.observe(host);
      resize();
    }, 0);
    return () => {
      alive = false;
      sendLine.current = () => false;
      clearTimeout(start); clearTimeout(fitTimer);
      observer?.disconnect(); socket?.close(); term?.dispose();
      host?.removeEventListener("paste", onHostPaste, true);
      terminal.current = null;
      refitSoon.current = () => {};
    };
  }, [operation]);
  useEffect(() => {
    if (!terminal.current) return;
    terminal.current.options.theme = xtermThemeFor(scheme, theme);
    terminal.current.options.fontSize = size;
    terminal.current.options.fontFamily = family;
    refitSoon.current();
  }, [theme, scheme, size, family]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const text = line.trim();
    if (text && sendLine.current(text)) setLine("");
  };
  return <div className={s.stack}>
    {link && <div className={s.row}>
      <a className={button.primary} href={link} target="_blank" rel="noopener noreferrer">
        Open the sign-in link · {new URL(link).host}
      </a>
    </div>}
    <div ref={node} className={s.terminal} aria-label="Temporary vendor sign-in terminal" />
    {ended ? <p className={s.note} role="status">The sign-in command ended. Its last screen stays above; start a new attempt to try again.</p>
      : <form className={s.row} onSubmit={submit}>
        <label className={s.field}><span>Code from the sign-in page</span>
          <input className={s.input} value={line} onChange={e => setLine(e.target.value)} autoComplete="off" autoCapitalize="off" spellCheck={false} />
        </label>
        <button className={button.ghost} type="submit" disabled={!line.trim()}>Send to sign-in</button>
      </form>}
  </div>;
}
