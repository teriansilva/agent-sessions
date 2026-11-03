import { type FormEvent, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { api } from "../../lib/api";
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

/** No session, persistence or logging. Unmount closes this one sign-in attempt (code 4000).
 *
 *  A LOST socket is not the end (#1846): a phone browser drops it while the operator approves the
 *  sign-in in another tab. The server keeps the vendor command for a grace window, so this
 *  terminal keeps its screen and attaches again — when the page is visible again, the network is
 *  back, or on a short backoff — and receives only what the vendor printed meanwhile.
 *
 *  When the vendor's command exits, the terminal STAYS with its last screen (`onEnded` lets the
 *  owner re-read the operation): the vendor's own reason for a failed sign-in ("Login failed: …")
 *  is the only diagnosis there is, and BattleLab keeps no copy of it. The last http(s) link the
 *  vendor printed is offered as a button, and a one-line field types a pasted code into the
 *  terminal: a 400-character OAuth URL wraps across five rows and is easy to break by hand. */
/** Close codes shared with `plugins/signin.py`. */
const SERVER_ENDED = 4409; // the sign-in ended (or could not start); read the operation
const CLOSED = 1000; // the vendor's command completed
const REFUSED = 4403; // not authenticated
const CLOSED_BY_OPERATOR = 4000; // this terminal closed it: end now, no grace
/** 1005 (closed with no code) is never the server's (it always names one): treated as an end. */
const ENDED = new Set([SERVER_ENDED, CLOSED, REFUSED, 1005]);
/** The server keeps a lost sign-in this long (`GRACE_SECONDS`); past it, reconnecting is moot. */
const GRACE_MS = 300_000;

export function SignInTerminal({ operation, onEnded }: { operation: string; onEnded: () => void }) {
  const node = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal | null>(null);
  const sendLine = useRef<(text: string) => boolean>(() => false);
  const refitSoon = useRef<() => void>(() => {});
  const close = useRef(onEnded);
  const [link, setLink] = useState<string | null>(null);
  const [ended, setEnded] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [line, setLine] = useState("");
  const { theme } = useTheme();
  const { scheme } = useTermScheme();
  const { size } = useTermSize();
  const { family } = useTermFont();
  const initial = useRef({ theme, scheme, size, family });
  useEffect(() => { close.current = onEnded; initial.current = { theme, scheme, size, family }; });
  useEffect(() => {
    let socket: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let lostAt: number | null = null;
    let connected = false; // a socket was ever opened: this attempt consumed the operation
    let finished = false; // the server named an outcome: nothing left to stop
    let attempt = 0;
    let reconnect: () => void = () => {};
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
      const url = `${protocol}//${location.host}/ws/plugins/signin/${encodeURIComponent(operation)}`;
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
      let scanTimer: ReturnType<typeof setTimeout> | undefined;
      const scan = () => {
        clearTimeout(scanTimer);
        scanTimer = setTimeout(() => {
          if (alive && term) setLink(lastUrl(term.buffer.active));
        }, 100);
      };
      const end = () => {
        finished = true;
        lostAt = null;
        setReconnecting(false);
        // Keep the last screen: the vendor's exit message is the only record of why it ended.
        term?.write("\r\n\x1b[2m— sign-in ended —\x1b[0m\r\n");
        setEnded(true);
        close.current();
      };
      const connect = () => {
        clearTimeout(retryTimer);
        connected = true;
        const own = openAppSocket(url);
        socket = own;
        own.binaryType = "arraybuffer";
        own.onopen = () => {
          if (socket !== own) return;
          lostAt = null; attempt = 0;
          setReconnecting(false);
          resize(); term?.focus();
        };
        own.onmessage = event => {
          if (alive && socket === own && event.data instanceof ArrayBuffer) term?.write(new Uint8Array(event.data), scan);
        };
        own.onclose = event => {
          if (!alive || socket !== own) return;
          socket = null;
          // The server's own verdicts (and an auth refusal) end the attempt. Anything else is a
          // lost connection, and BUSY means another socket still holds it: try again while the
          // server's grace window can still be open.
          if (ENDED.has(event.code)) return end();
          lostAt ??= Date.now();
          if (Date.now() - lostAt > GRACE_MS) return end();
          setReconnecting(true);
          retryTimer = setTimeout(connect, Math.min(10_000, 1000 * 2 ** attempt++));
        };
      };
      reconnect = () => {
        if (alive && socket === null && lostAt !== null && document.visibilityState === "visible") connect();
      };
      document.addEventListener("visibilitychange", reconnect);
      window.addEventListener("online", reconnect);
      connect();
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
      clearTimeout(start); clearTimeout(fitTimer); clearTimeout(retryTimer);
      document.removeEventListener("visibilitychange", reconnect);
      window.removeEventListener("online", reconnect);
      // An explicit close ends the sign-in at once, with no grace window. A 4000 close only
      // reaches the server from the socket that owns it, and this one may be between sockets,
      // still connecting, or about to be refused BUSY (Hermes on #1849): the authenticated cancel
      // route says it as well. Not for an attempt that never connected (StrictMode's abandoned
      // first mount must leave the one-use operation ready) or one that already ended.
      if (connected && !finished) {
        socket?.close(CLOSED_BY_OPERATOR, "closed");
        void api.pluginCancel(operation).catch(() => {});
      }
      observer?.disconnect(); term?.dispose();
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
    {reconnecting && !ended && <p className={s.note} role="status" data-testid="signin-reconnecting">Connection lost. Reconnecting to the sign-in…</p>}
    {ended ? <p className={s.note} role="status">The sign-in command ended. Its last screen stays above; start a new attempt to try again.</p>
      : <form className={s.row} onSubmit={submit}>
        <label className={s.field}><span>Code from the sign-in page</span>
          <input className={s.input} value={line} onChange={e => setLine(e.target.value)} autoComplete="off" autoCapitalize="off" spellCheck={false} />
        </label>
        <button className={button.ghost} type="submit" disabled={!line.trim()}>Send to sign-in</button>
      </form>}
  </div>;
}
