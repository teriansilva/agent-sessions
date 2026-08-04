// Browser app-mode mount (#579 P4b). Wires a Home Free tunnel into the app's seams and
// mounts the real BattleLab SPA over it, so the whole UI streams from the box through the
// blind relay. Fork C (SPA mount) is the **dynamic-import** approach: the app bundle is
// imported on demand and rendered into the connect page — it boots exactly like main.tsx.
//
// The tunnel-wiring + WebSocket backstop are separated from the React mount so they can be
// unit-tested; the actual full-app render + session-switching is proven in the P7 real-browser
// E2E.

import "../index.css"; // the app's base tokens/styles so the mounted SPA is styled

import { setApiFetch } from "../lib/api";
import { setWsFactory } from "../lib/termSocket";
import {
  type AppSessionHandle,
  type SessionEvent,
  type SocketLike,
  runAppSession,
} from "./connect";
import { createTunnel, type Tunnel } from "./tunnel";

function selfHost(): string | undefined {
  return typeof location !== "undefined" ? location.host : undefined;
}

/** True for the app's OWN sockets (**same-origin** `/ws…`) — only those ride the tunnel; a
 *  genuinely external WebSocket (any other host, or a non-`/ws` path) keeps the real
 *  constructor. `host` is the connect page's own host (defaults to `location.host`). */
export function isAppSocketUrl(
  url: string,
  host: string | undefined = selfHost(),
): boolean {
  if (url.startsWith("/")) return url.startsWith("/ws"); // relative → same-origin by definition
  try {
    const u = new URL(url); // absolute ws(s)://host/path
    return u.pathname.startsWith("/ws") && !!host && u.host === host; // must be OUR origin
  } catch {
    return false;
  }
}

interface WsGlobal {
  WebSocket: typeof WebSocket;
}

/** Install a scoped `window.WebSocket` backstop for any socket that doesn't go through the
 *  `termSocket` `wsFactory` seam: app `/ws…` sockets ride the tunnel, everything else uses the
 *  real constructor. Returns a restore fn — call it on teardown so the shim never outlives the
 *  connect session (it must not proxy unrelated external sockets afterwards). */
export function installWsBackstop(
  tunnel: Tunnel,
  win: WsGlobal = globalThis as unknown as WsGlobal,
  host: string | undefined = selfHost(),
): () => void {
  const real = win.WebSocket;
  const shim = function (
    url: string | URL,
    protocols?: string | string[],
  ): WebSocket {
    return isAppSocketUrl(String(url), host)
      ? (tunnel.wsFactory(String(url)) as unknown as WebSocket)
      : new real(url, protocols);
  } as unknown as typeof WebSocket;
  Object.assign(shim, {
    CONNECTING: real.CONNECTING,
    OPEN: real.OPEN,
    CLOSING: real.CLOSING,
    CLOSED: real.CLOSED,
  });
  win.WebSocket = shim;
  return () => {
    win.WebSocket = real;
  };
}

export interface WiredTunnel {
  tunnel: Tunnel;
  session: AppSessionHandle;
  /** Restore the WebSocket global, clear the injected fetch/wsFactory, close the tunnel + session. */
  teardown: () => void;
}

/**
 * Establish an app-mode session and wire its tunnel into the app's seams — the injected
 * `fetch` (`setApiFetch`), the terminal `wsFactory` (`setWsFactory`), and the scoped global
 * `WebSocket` backstop. Does NOT mount React (see {@link mountApp}); split out so it's testable.
 */
export async function wireAppTunnel(
  ws: SocketLike,
  accessKey: string,
  captcha: string,
  onEvent?: (e: SessionEvent) => void,
): Promise<WiredTunnel> {
  // Feed inbound frames into the tunnel; the holder lets `onFrame` reference the tunnel that
  // is created right after runAppSession returns (its recv loop is suspended on the first
  // await, so no frame is delivered before the assignment below).
  const holder: { tunnel: Tunnel | null } = { tunnel: null };
  const session = await runAppSession(ws, accessKey, captcha, {
    onFrame: (f) => holder.tunnel?.feed(f),
    onEvent: (e) => onEvent?.(e),
  });
  const tunnel = createTunnel(session.sendFrame);
  holder.tunnel = tunnel;

  setApiFetch(tunnel.fetch);
  setWsFactory((url) => tunnel.wsFactory(url) as unknown as WebSocket);
  const restoreWs = installWsBackstop(tunnel);

  const teardown = () => {
    restoreWs();
    setApiFetch(null);
    setWsFactory(null);
    tunnel.close();
    session.close();
  };
  return { tunnel, session, teardown };
}

export interface MountedApp {
  teardown: () => void;
}

/** Renders the SPA into `rootId` and returns an unmount fn. Injectable for testing. */
export type RenderApp = (rootId: string) => Promise<() => void>;

/** Fork C (dynamic-import): pull the app bundle on demand and boot it exactly like main.tsx. */
const dynamicImportRender: RenderApp = async (rootId) => {
  const [
    { default: App },
    { createRoot },
    react,
    { bootTheme },
    { bootAccent },
  ] = await Promise.all([
    import("../app/App"),
    import("react-dom/client"),
    import("react"),
    import("../theme/applyTheme"),
    import("../theme/applyAccent"),
  ]);
  bootTheme();
  bootAccent();
  const el = document.getElementById(rootId);
  if (!el) throw new Error("mountApp: root element missing");
  const root = createRoot(el);
  root.render(
    react.createElement(react.StrictMode, null, react.createElement(App)),
  );
  return () => root.unmount();
};

/**
 * Full app-mode connect: wire the tunnel, then mount the real SPA over it. If the mount step
 * fails (a stale/blocked chunk, missing root, boot error), the tunnel/seams/shim/session are
 * always torn down so `connectApp()`'s retry can't double-install.
 */
export async function mountApp(
  ws: SocketLike,
  accessKey: string,
  captcha: string,
  opts: {
    rootId?: string;
    onEvent?: (e: SessionEvent) => void;
    render?: RenderApp;
  } = {},
): Promise<MountedApp> {
  const wired = await wireAppTunnel(ws, accessKey, captcha, opts.onEvent);
  try {
    const unmount = await (opts.render ?? dynamicImportRender)(
      opts.rootId ?? "app-root",
    );
    return {
      teardown: () => {
        unmount();
        wired.teardown();
      },
    };
  } catch (e) {
    wired.teardown();
    throw e;
  }
}
