import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { Terminal as Xterm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { ArrowDown, ArrowLeftRight, PanelRight, RotateCw, ScrollText } from "lucide-react";
import {
  type CSSProperties,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { api } from "../../lib/api";
import {
  engineBadge,
  engineName,
  projectColor,
  relTime,
  shortCwd,
} from "../../lib/format";
import { createBootReadyGate } from "../../lib/bootReady";
import { getBrowserFp, getTabId } from "../../lib/browserFp";
import { getDeviceLabel } from "../../lib/deviceLabel";
import { HistoryLoader, type HistoryState } from "../../lib/historyLoader";
import { PagesBuffer, foldWipe } from "../../lib/pagesBuffer";
import {
  imageFilesFromAsyncClipboard,
  imageFilesFromData,
} from "../../lib/clipboardImages";
import { isCopyShortcut, isPasteShortcut } from "../../lib/termKeys";
import { urlAtCell } from "../../lib/linkHitTest";
import {
  decideMouseDown,
  exceededSlop,
  forceSelectModifier,
} from "../../lib/termSelect";
import { useConfig } from "../../app/config";
import { useSessionsStore } from "../../app/sessionsStore";
import {
  TermSocket,
  type TermGateHolder,
  type TermRole,
  type TermStatus,
} from "../../lib/termSocket";
import { type FreshSession, termWsUrl } from "../../lib/termUrl";
import { appConsumesWheel, attachTouchScroll } from "../../lib/touchScroll";
import { useAccent } from "../../theme/accentStore";
import { THEMES, xtermTheme } from "../../theme/themes";
import { useTheme } from "../../theme/themeStore";
import { Compose, type ComposeHandle } from "./Compose";
import { HandoffModal } from "./HandoffModal";
import { HeadActions, type HeadAction } from "./HeadActions";
import { SessionRecapModal } from "./SessionRecapModal";
import styles from "./Terminal.module.css";

// #554: how long the auto copy-on-select "Copied" toast stays up (matches the CSS fade).
const COPIED_TOAST_MS = 1200;
// #617: the failure toast asks the operator to do something ("use a secure origin"), so it needs
// longer to read than the success flash. Matches .copyFailed's animation-duration.
const COPY_FAILED_TOAST_MS = 2600;

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

/** Persistent HUD panel-header readout (#211 4c): the LED class for the reusable .hud-led
 *  primitive plus its label. Distinct from statusText (the transient corner overlay) — this one
 *  is always present so the terminal panel always declares its link state.
 *
 *  #744: the label is no longer PRINTED beside the LED (the old "STATUS // LIVE" run spent seven
 *  characters restating the dot). It moved to the LED's accessible name + tooltip instead of
 *  being deleted — link state must never be carried by colour alone. */
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
  filesOpen,
  onToggleFiles,
  filesDisabledReason,
}: {
  engine: string;
  id: string;
  fresh?: FreshSession;
  /** File panel (#783). The panel itself is owned by SessionView (it lays out beside this pane);
   *  the terminal only carries its trigger, because the pane head is where the trigger belongs. */
  filesOpen?: boolean;
  onToggleFiles?: (trigger?: HTMLElement | null) => void;
  /** When set, the Files action renders DISABLED with this as its tooltip rather than vanishing
   *  — a session still resolving its cwd should say so, not silently lose the control. */
  filesDisabledReason?: string;
  /** Server reconciled to the real engine-qualified id (#127, opencode new-session).
   *  The owner converges the URL/sidebar without tearing down the socket. */
  onReconcileId?: (sid: string) => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const fabRef = useRef<HTMLButtonElement>(null);
  // Halts an in-flight touch-momentum glide (set by attachTouchScroll). The FAB's jump-to-
  // tail calls it first, or leftover fling velocity from the scroll-up drag drags the view
  // straight back off the tail (#519 follow-up — the FAB paints above the touch overlay and
  // takes the tap itself, so the overlay's own stopFling never runs for it).
  const stopMomentumRef = useRef<() => void>(() => {});
  const sockRef = useRef<TermSocket | null>(null);
  const composeRef = useRef<ComposeHandle>(null);
  const termRef = useRef<Xterm | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  // Manual repaint (#485): the live socket effect publishes its rows−1→rows nudge here so the
  // owner-only REPAINT header button can force a winch-repaint agent to redraw its current frame
  // WITHOUT killing the process (unlike RESTART). Reset to a no-op on teardown so a stale click
  // can't resize a disposed socket.
  const jiggleRef = useRef<() => void>(() => {});
  const { theme } = useTheme();
  const { accent } = useAccent();
  // Resolve the human session title for the panel header (#232) from the shared store the
  // sidebar fills (matched by engine+uuid). Falls back to a short id before the
  // list has loaded / for a fresh placeholder session. Read-only: it never re-keys the socket.
  const { sessions } = useSessionsStore();
  const [status, setStatus] = useState<TermStatus>({ kind: "connecting" });
  const [coarse] = useState(
    () => window.matchMedia?.("(pointer: coarse)")?.matches ?? false,
  );
  // Compose default state (#254): the per-user pref overrides the device heuristic. "auto"
  // (and an unloaded config) keeps the heuristic — expanded on touch, collapsed on desktop.
  const composeMode = useConfig()?.compose_default ?? "auto";
  const composeDefaultOpen =
    composeMode === "open"
      ? true
      : composeMode === "collapsed"
        ? false
        : coarse;
  // Mobile scroll-to-bottom FAB (#187): shown when the viewport has been scrolled
  // up off the live tail. Updated from xterm's onScroll; the click jumps back.
  const [atBottom, setAtBottom] = useState(true);
  // Scroll-to-bottom for app-consuming sessions (#559). A mouse-tracking TUI (claude arms
  // ?1000/?1002/?1003; opencode via mouse+alt) owns its OWN scroll, so xterm's buffer never leaves
  // the tail — `atBottom` stays true and the FAB would never show, and `scrollToBottom()` is a
  // no-op. Track how far we've forwarded the agent up (in wheel notches) so the FAB appears once the
  // user scrolls the agent up, and so the jump-to-tail forwards the same distance back down.
  // (codex/gemini run inline with NO mouse tracking → they keep real scrollback and use the #187
  // path above, unchanged.) `jumpToTailRef` is filled by the effect from attachTouchScroll.
  const [appScrolledUp, setAppScrolledUp] = useState(false);
  const appScrollNotchesRef = useRef(0);
  // #584: a FRESH attach to an app-consuming (mouse-tracking) session opens wherever claude last
  // repainted — frequently NOT its live tail — and we can't measure the agent's own scroll. Since
  // xterm's buffer stays pinned at the tail (`atBottom` true) and `appScrolledUp` is false until
  // the user scrolls up, the ↓ FAB would be hidden with no way back to the prompt. This flag shows
  // the FAB from attach until the user reaches / requests the tail. Ref mirrors the state so the
  // jump handlers (defined outside the effect) can read + size a robust jump-to-tail.
  const [appTailUnknown, setAppTailUnknown] = useState(false);
  const appTailUnknownRef = useRef(false);
  const jumpToTailRef = useRef<(notches: number) => void>(() => {});
  // Auto copy-on-select "Copied" toast (#554): 0 = hidden, else a monotonic tick used as the
  // element key so each copy restarts the fade animation. Set by the mouseup copy-on-settle handler.
  // #554 copy-on-select toast; `ok` false = the clipboard write failed (insecure origin) (#617).
  const [copied, setCopied] = useState<{ tick: number; ok: boolean }>({
    tick: 0,
    ok: true,
  });
  // Scroll-up lazy-load (#348 Phase 3): pill state (loading / start-of-history / error)
  // + whether the viewport sits at the very top of the scrollback (the end pill only
  // shows there). `histRetryRef` holds the effect-scoped retry closure for the error pill.
  const [histState, setHistState] = useState<HistoryState>("idle");
  const [atTop, setAtTop] = useState(false);
  const histRetryRef = useRef<() => void>(() => {});
  // Per-tab ownership (#184 slice 3): the server's verdict on whether this WS
  // bridge holds the owner role or is a read-only secondary. Default is "owner"
  // until the server says otherwise — backward-compatible with the pre-slice-3
  // server which never sends a role frame at all.
  const [role, setRole] = useState<TermRole>("owner");
  // Read-only take-over banner (#293/#434, flag on): the active viewer's identity when this
  // tab is a read-only secondary — it opened a session already active elsewhere, or it was
  // taken over mid-session. null = we're the owner / not gated. The PTY stream keeps flowing
  // either way (#434): a secondary is read-only, never blank.
  const [holder, setHolder] = useState<TermGateHolder | null>(null);
  // Bumped to tear down + reopen the socket via the Take-over path (#184). Whether the fresh connect
  // demands ?force=1 is decided ONLY by `forceNextConnectRef` (set by takeover, consumed by the
  // effect) — NOT by which epoch moved.
  const [takeoverEpoch, setTakeoverEpoch] = useState(0);
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

  // First-compose gate for a FRESH launch (#533/#607/#616). The agent boots for several seconds
  // after new=1; input written into that window is swallowed (the composed text) or submitted as
  // garbage — in the incident the literal Ctrl-A of the compose clear became the whole first turn.
  // "Input ready" = the boot output has gone QUIET (see lib/bootReady). The bracketed-paste enable
  // (ESC[?2004h) only shortens the quiet window — it is not itself readiness, because claude emits
  // it before switching to the alternate screen and clearing it, wiping anything pasted on it.
  // Attaches to already-running sessions are ready immediately.
  const inputReadyRef = useRef<boolean>(!fresh);
  const readyWaitersRef = useRef<Array<() => void>>([]);
  const markInputReady = useCallback(() => {
    if (inputReadyRef.current) return;
    inputReadyRef.current = true;
    for (const w of readyWaitersRef.current.splice(0)) w();
  }, []);
  // Compose calls this before its first delivery: `true` (synchronous — the common case, so
  // the established clear→paste→deferred-Enter sequencing is untouched) or a promise that
  // resolves true on readiness / false when `timeoutMs` expires first.
  const waitInputReady = useCallback(
    (timeoutMs: number): true | Promise<boolean> => {
      if (inputReadyRef.current) return true;
      return new Promise<boolean>((resolve) => {
        const t = setTimeout(() => resolve(false), timeoutMs);
        readyWaitersRef.current.push(() => {
          clearTimeout(t);
          resolve(true);
        });
      });
    },
    [],
  );

  // Send raw input to the PTY (used by the mobile action bar / compose). Returns whether the frame
  // was actually delivered (socket OPEN) — Compose uses this so it never submits a bare Enter after
  // a clear/paste that got dropped mid-reconnect (the empty-compose bug #287).
  const sendInput = useCallback(
    (d: string) => sockRef.current?.send({ t: "i", d }) ?? false,
    [],
  );
  // Current socket id (bumped each reconnect) so Compose can detect a reconnect between its frames.
  const connEpoch = useCallback(() => sockRef.current?.connectionId ?? -1, []);
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    // Fresh terminal instance / reconnect → the agent is at its live tail; clear any app-scroll
    // state so a stale count from a prior connection can't keep the FAB up (#559).
    appScrollNotchesRef.current = 0;
    setAppScrolledUp(false);
    // #584: start each connection with the tail "known" — the fresh-attach FAB is (re)armed only by
    // armRepaintBackstop below, for a true fresh attach (consumed === 0) once mouse tracking is seen.
    setAppTailUnknown(false);
    appTailUnknownRef.current = false;

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
      // #617: on macOS xterm's `shouldForceSelection` is `altKey && macOptionClickForcesSelection`
      // (Shift is inert there), so without this NO modifier — synthetic or real — can select while
      // the agent owns the mouse. We only ever synthesize Alt ourselves (see lib/termSelect); the
      // cost is that a real Alt+click now forces a selection on Mac instead of reaching the app.
      macOptionClickForcesSelection: true,
      // …which makes xterm's default `altClickMovesCursor: true` actively dangerous: its mouseup
      // handler fires on `altKey` with a <=1-char selection and writes cursor-move ARROW KEYS to
      // the PTY. Alt is now the Mac selection modifier, so an Alt+click would both select AND
      // inject keystrokes into the agent. Alt selects; it never moves the cursor.
      altClickMovesCursor: false,
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

    // Hide xterm's native scrollbar on sessions where the AGENT owns the scroll. A mouse-tracking
    // TUI (claude/opencode) has the wheel forwarded to it, so xterm's viewport never leaves the
    // tail — the bar sits stuck at the bottom and can't reflect the agent's own scroll, it just
    // misleads. An alt-screen app keeps no usable scrollback either. `appConsumesWheel` covers both;
    // a plain scrollback session (codex/gemini/antigravity) keeps its working bar. Re-checked after
    // every parsed write because mouse mode is armed mid-stream (and re-emitted on attach by #397).
    // The wheel still scrolls inside the agent, and the ↓ FAB (#559) jumps back to the tail.
    let ownsScroll = "";
    const syncScrollbarChrome = () => {
      const v = appConsumesWheel(term) ? "true" : "false";
      if (v === ownsScroll) return;
      ownsScroll = v;
      host.dataset.appOwnsScroll = v;
    };
    syncScrollbarChrome();
    const writeParsedSub = term.onWriteParsed(syncScrollbarChrome);

    // Don't forward the paste shortcut (Ctrl+V / Cmd+V) to the PTY as a raw keystroke
    // (#209): the agent (Claude Code) binds Ctrl+V to "paste image from clipboard" and
    // reads the SERVER clipboard, printing "no image found in clipboard" on a text paste.
    // Returning false makes xterm skip the key WITHOUT preventDefault, so the browser's
    // native paste still fires → onHostPaste → term.paste(text), one clean paste.
    const isMac = /mac|iphone|ipad/i.test(
      navigator.platform || navigator.userAgent || "",
    );
    term.attachCustomKeyEventHandler((e) => {
      if (isPasteShortcut(e, isMac)) return false;
      // Ctrl+C / Ctrl+Shift+C with an active selection COPIES it (#536) — the Windows
      // Terminal convention. The key never reaches the PTY: an accidental ^C while trying
      // to copy is a SIGINT that can kill the agent's running turn. Without a selection
      // Ctrl+C stays the interrupt it always was. The key is consumed even if the async
      // clipboard write later fails (secure-context-only edge) — falling through to a
      // SIGINT would be strictly worse than a failed copy. preventDefault so the browser's
      // own copy command doesn't double-fire on the mirrored DOM selection.
      if (isCopyShortcut(e) && term.hasSelection()) {
        void navigator.clipboard
          ?.writeText(term.getSelection())
          .catch(() => {});
        e.preventDefault();
        return false;
      }
      return true;
    });

    // Selection vs the agent's mouse (#536/#582/#617). A mouse-tracking agent (claude, opencode —
    // both alt-screen since claude 2.1.178; ?1000h/?1002h/?1003h re-emitted on every attach by
    // #397) makes xterm route an unmodified left-press to the app, so a plain drag selects nothing.
    // We arbitrate by GESTURE, not by buffer type: a press is swallowed until the pointer either
    // moves (a drag → force a selection at the anchor) or lifts in place (a click → replay the
    // press so the TUI's clickable UI still works). Sessions with no mouse tracking are left to
    // xterm's native selection. See lib/termSelect for the decision table and the macOS modifier.
    const { shiftKey: twinShift, altKey: twinAlt } = forceSelectModifier(isMac);
    /** Re-dispatch a press at (x, y) — as the force-selection twin, or as a plain replayed click.
     *  Synthetic ⇒ untrusted ⇒ `onTermMouseDown` lets it through to xterm untouched. */
    const dispatchPress = (
      target: EventTarget,
      src: {
        screenX: number;
        screenY: number;
        clientX: number;
        clientY: number;
        detail: number;
      },
      opts: { force: boolean },
    ) =>
      target.dispatchEvent(
        new MouseEvent("mousedown", {
          bubbles: true,
          cancelable: true,
          composed: true,
          view: window,
          detail: src.detail,
          screenX: src.screenX,
          screenY: src.screenY,
          clientX: src.clientX,
          clientY: src.clientY,
          button: 0,
          buttons: 1,
          ...(opts.force ? { shiftKey: twinShift, altKey: twinAlt } : {}),
        }),
      );

    /** A swallowed press awaiting its drag-vs-click verdict. */
    let pending: {
      target: EventTarget;
      screenX: number;
      screenY: number;
      clientX: number;
      clientY: number;
      detail: number;
    } | null = null;

    const onTermMouseDown = (e: MouseEvent) => {
      const decision = decideMouseDown(e, {
        mouseTracking: (term.modes?.mouseTrackingMode ?? "none") !== "none",
      });
      if (decision === "native") return;
      e.preventDefault();
      e.stopImmediatePropagation();
      if (!e.target) return;
      // detail > 1: word/line select now — no drag is coming.
      if (decision === "force-select") {
        dispatchPress(e.target, e, { force: true });
        return;
      }
      pending = {
        target: e.target,
        screenX: e.screenX,
        screenY: e.screenY,
        clientX: e.clientX,
        clientY: e.clientY,
        detail: e.detail,
      };
    };

    // Pointer moved past the slop → it was a drag. Force the selection AT THE ANCHOR; xterm's
    // SelectionService then binds its own move/up listeners and extends from the real mousemoves.
    const onGestureMove = (e: MouseEvent) => {
      if (!pending || !e.isTrusted) return;
      if (!exceededSlop(pending.clientX, pending.clientY, e.clientX, e.clientY))
        return;
      const anchor = pending;
      pending = null;
      dispatchPress(anchor.target, anchor, { force: true });
    };

    // Released without moving → it was a click. Replay the plain press so the app sees it; the real
    // (trusted) mouseup keeps propagating, so the TUI gets its release and copy-on-select still runs.
    const onGestureUp = () => {
      if (!pending) return;
      const anchor = pending;
      pending = null;
      dispatchPress(anchor.target, anchor, { force: false });
    };

    host.addEventListener("mousedown", onTermMouseDown, true);
    document.addEventListener("mousemove", onGestureMove, true);
    document.addEventListener("mouseup", onGestureUp, true);
    const vpEl = host.querySelector<HTMLElement>(".xterm-viewport");

    // #187: track whether the viewport is sitting at the live tail. xterm fires
    // onScroll with the topmost line of the viewport whenever the user scrolls or
    // new output pushes the buffer; "at bottom" means viewportY has caught up to
    // baseY (the bottom of the scrollback). Eight-line dead zone so a single
    // wheel click while live output is streaming doesn't flicker the FAB on/off.
    const SCROLL_DEAD_ZONE = 8;
    const computeDomAtBottom = () => {
      if (!vpEl) return true;
      const rowHeight = vpEl.clientHeight / Math.max(1, term.rows || 24);
      const deadPx = SCROLL_DEAD_ZONE * Math.max(1, rowHeight);
      return vpEl.scrollHeight - vpEl.clientHeight - vpEl.scrollTop <= deadPx;
    };
    const computeAtBottom = () => {
      const buf = term.buffer?.active;
      if (!buf) return true;
      return (
        buf.baseY - buf.viewportY <= SCROLL_DEAD_ZONE && computeDomAtBottom()
      );
    };
    const updateAtBottom = () => setAtBottom(computeAtBottom());

    // --- Scroll-up lazy-load (#348 Phase 3) ----------------------------------------
    // xterm.js cannot prepend into an existing buffer, so older pages live in a client-
    // side buffer and the whole terminal is RE-WRITTEN on each prepend (the issue-
    // sanctioned fallback, only ever triggered at the very top of the scrollback):
    // reset → fetched pages (oldest-first) → a viewport of blank lines → the recorded
    // live stream. The blank gap pushes the pages fully into scrollback so the stream's
    // leading clear (ESC[2J) can't eat the page tail — the same framing the server's
    // transcript attach payload uses. ESC[3J (clear-scrollback) is stripped from the
    // replay: in the original it wiped pre-attach junk; replayed it would wipe the
    // prepended pages. After the rewrite the viewport is re-anchored so the previously-
    // top visible line stays put. Pills are OVERLAYS (see JSX), never buffer rows.
    const STREAM_BUF_CAP = 2 * 1024 * 1024; // chars; same order as the server ring cap
    // Fetched older pages are BOUNDED too (Hermes #365): ~8 server pages at the default
    // 512 KiB page-bytes cap. The cap is a VISIBLE floor, not a rolling window (r2): when
    // the next page won't fit, the loader latches "capped" — the "older history beyond
    // local cap" pill — instead of evict-rewind-refetching the same page forever. A
    // server scrollback wipe (ESC[3J) resets the buffer + loader and lifts the latch.
    const PAGES_BUF_CAP = 4 * 1024 * 1024; // chars
    const streamDecoder = new TextDecoder();
    let streamBuf = ""; // everything the socket delivered, decoded — the rewrite source
    const rewriteQueue: Uint8Array[] = []; // live chunks held back while a rewrite is in flight
    let attachReplayOpen = true;
    let codexEraseCarry = new Uint8Array(0);
    const SCROLLBACK_ERASE = new Uint8Array([0x1b, 0x5b, 0x33, 0x4a]); // CSI 3J
    const stripCodexLiveScrollbackErase = (b: Uint8Array) => {
      if (engine !== "codex" || attachReplayOpen) {
        codexEraseCarry = new Uint8Array(0);
        return b;
      }
      let src = b;
      const hadCarry = codexEraseCarry.length > 0;
      if (codexEraseCarry.length) {
        src = new Uint8Array(codexEraseCarry.length + b.length);
        src.set(codexEraseCarry, 0);
        src.set(b, codexEraseCarry.length);
        codexEraseCarry = new Uint8Array(0);
      }
      const out: number[] = [];
      for (let i = 0; i < src.length;) {
        const remaining = src.length - i;
        const full =
          remaining >= SCROLLBACK_ERASE.length &&
          SCROLLBACK_ERASE.every((v, j) => src[i + j] === v);
        if (full) {
          i += SCROLLBACK_ERASE.length;
          continue;
        }
        const partial =
          remaining < SCROLLBACK_ERASE.length &&
          SCROLLBACK_ERASE.slice(0, remaining).every(
            (v, j) => src[i + j] === v,
          );
        if (partial) {
          codexEraseCarry = src.slice(i);
          break;
        }
        out.push(src[i]);
        i++;
      }
      return !hadCarry &&
        out.length === src.length &&
        codexEraseCarry.length === 0
        ? b
        : new Uint8Array(out);
    };
    // Blank-attach repaint backstop (#349 follow-up, operator report): some idle
    // sessions paint fragments or nothing on selection — the server-side nudge can be
    // coalesced/missed, and only a REAL geometry change reliably makes winch-repaint
    // agents redraw. When an attach delivers (almost) no bytes OR the visible xterm
    // rows are still blank after a large replay (#407), the CLIENT jiggles rows−1 →
    // rows. Rows-only on purpose: a width change would reset the scrollback
    // ring / dirty the VT mirror. The client owns the resize channel, so nothing can
    // interleave inside its pair (unlike the server nudge racing the connect resize),
    // and the spacing exceeds the agents' resize debounce → two distinct repaints.
    let attachBytes = 0;
    let initialTailLock = false;
    let jiggleTimers: ReturnType<typeof setTimeout>[] = [];
    const clearJiggle = () => {
      for (const t of jiggleTimers) clearTimeout(t);
      jiggleTimers = [];
    };
    const visibleRowsBlank = () => {
      const rows = host.querySelector<HTMLElement>(".xterm-rows");
      return (rows?.textContent ?? "").trim().length === 0;
    };
    // #416: the fragment case — a SUBSTANTIAL replay was processed but only the top handful of
    // rows rendered, leaving most of a tall grid blank (operator screenshot: a few lines of a
    // Claude frame, the rest empty, self-healing on the agent's next repaint). visibleRowsBlank
    // is false (there IS text) so the #407 guard alone never repaints it. "Sparse" = only a small
    // fraction of the grid's rows carry content — distinct from a legitimately short prompt, which
    // pairs few rows with a SMALL replay (gated by FRAGMENT_MIN_BYTES below), and from a full TUI
    // frame, which fills the grid.
    const visibleRowsSparse = () => {
      const rows = host.querySelector<HTMLElement>(".xterm-rows");
      if (!rows) return false;
      const total = term.rows || rows.children.length || 24;
      let nonEmpty = 0;
      for (const r of Array.from(rows.children)) {
        if ((r.textContent ?? "").trim().length) nonEmpty++;
      }
      return nonEmpty > 0 && nonEmpty < Math.max(6, Math.floor(total * 0.2));
    };
    const jiggleRows = () => {
      if (term.rows <= 4) return;
      sock.send({ t: "r", cols: term.cols, rows: term.rows - 1 });
      jiggleTimers.push(
        setTimeout(() => {
          if (sock !== sockRef.current) return;
          sock.send({ t: "r", cols: term.cols, rows: term.rows });
        }, 320),
      );
    };
    // Publish the nudge so the REPAINT button can invoke it from render (#485). jiggleRows already
    // guards rows>4 and sock===sockRef.current, so a click on a superseded socket is a no-op.
    jiggleRef.current = jiggleRows;
    const armRepaintBackstop = () => {
      attachBytes = 0;
      initialTailLock = true;
      clearJiggle();
      jiggleTimers.push(
        setTimeout(() => {
          if (sock !== sockRef.current) return;
          // The initial attach replay is over: release the tail lock so steady-state follow is
          // governed purely by viewport position (computeAtBottom). Without this the lock would
          // only ever clear on a wheel/touch/key gesture, so a scrollbar-drag scroll-up was
          // dragged back to the bottom by the next output chunk (the "always jumps to bottom" bug).
          initialTailLock = false;
          // #584: the initial attach replay has settled, so the agent's private modes are now known
          // (the server re-emits them at the very start of the replay, #397). If this is an
          // app-consuming session (mouse-tracking claude / alt-screen TUI), the agent owns its scroll
          // and may have opened off its live tail — reveal the ↓ FAB so the user has a one-tap jump
          // back. Gated on mode status HERE (post-attach), never assumed at mount. Cleared by a user
          // gesture (armHistory) or the jump itself (scrollToTail). No-op for a scrollback session
          // (codex/antigravity: appConsumesWheel false → the FAB stays driven by computeAtBottom).
          if (appConsumesWheel(term)) {
            setAppTailUnknown(true);
            appTailUnknownRef.current = true;
          }
          // "Blank" used to mean "essentially no replay bytes". #407 shows the
          // byte count is not enough: a large raw replay can process successfully
          // while xterm's visible row layer remains empty. In that case, repaint too.
          // #416 extends this: a large replay can also leave only a SPARSE fragment
          // painted (top rows filled, the rest blank) — repaint that too. The big-bytes
          // gate keeps a legitimately short prompt (few rows, small replay) from jiggling.
          const FRAGMENT_MIN_BYTES = 4096;
          const fragment =
            attachBytes >= FRAGMENT_MIN_BYTES && visibleRowsSparse();
          if (attachBytes >= 512 && !visibleRowsBlank() && !fragment) return;
          jiggleRows();
        }, 800),
      );
    };
    const pagesBuf = new PagesBuffer(PAGES_BUF_CAP); // fetched older pages, oldest-first
    let rewriting = false;
    const loader = new HistoryLoader(
      (q) => api.history(`${engine}:${id}`, { before: q.before, cols: q.cols }),
      setHistState,
    );
    const recordOutput = (b: Uint8Array) => {
      streamBuf += streamDecoder.decode(b, { stream: true });
      // A server-sent scrollback wipe (ESC[3J — clean-load / transcript re-render) means
      // everything before it is no longer on screen; keep only the post-wipe stream (as
      // a plain screen clear) so a rewrite reproduces what the user actually sees. The
      // fetched pages are part of that cleared scrollback: purge them and reset the
      // loader, or the next rewrite would resurrect what the server cleared (#365).
      const { buf, wiped } = foldWipe(streamBuf);
      streamBuf = buf;
      if (wiped) {
        pagesBuf.clear();
        loader.reset();
      }
      if (streamBuf.length > STREAM_BUF_CAP) {
        // Trim at a line boundary so a sliced ANSI sequence can't garble a rewrite.
        const cut = streamBuf.indexOf("\n", streamBuf.length - STREAM_BUF_CAP);
        streamBuf =
          cut >= 0
            ? streamBuf.slice(cut + 1)
            : streamBuf.slice(-STREAM_BUF_CAP);
      }
    };
    setHistState("idle");
    setAtTop(false);
    const prependPage = (ansi: string) => {
      if (!pagesBuf.prepend(ansi)) {
        // Depth cap reached (Hermes #365 r2): retaining this page would mean evicting it
        // straight back out (the new page IS the deepest — see PagesBuffer). The old
        // rewind-and-discard looped: still at the top, same page refetched, no visible
        // progress. Latch instead: the cap pill replaces the start-of-history pill and
        // auto-fetching stops until a server wipe resets the buffer + loader.
        loader.latchCap();
        return;
      }
      const before = term.buffer.active.length;
      rewriting = true;
      term.reset();
      // Honest seam (#348): the pages above are a TRANSCRIPT render while everything
      // below is the live byte replay — two sources with no shared coordinate, so up
      // to a page of turns can legitimately appear on both sides when the attach was
      // served from the VT mirror/ring. Mark the boundary instead of pretending the
      // buffer is one continuous stream.
      // Inline start-of-history rule (operator report): the overlay pill only shows
      // at the absolute viewport top, but the point where history BEGINS should be
      // visible in the buffer itself while scrolling past it — same idiom as the
      // transcript seam below. Included once the loader has latched "end" (the page
      // that exhausted history is part of THIS rewrite, so the rule lands with it).
      const startRule =
        loader.state === "end"
          ? (() => {
              const lbl = " start of history ";
              const f = Math.max(4, term.cols - lbl.length);
              return (
                "\x1b[38;5;240m" +
                "─".repeat(Math.floor(f / 2)) +
                lbl +
                "─".repeat(Math.ceil(f / 2)) +
                "\x1b[0m\r\n"
              );
            })()
          : "";
      const seamLabel = " older history ↑ (transcript) ";
      const fill = Math.max(4, term.cols - seamLabel.length);
      const seam =
        "\x1b[38;5;240m" +
        "─".repeat(Math.floor(fill / 2)) +
        seamLabel +
        "─".repeat(Math.ceil(fill / 2)) +
        "\x1b[0m";
      const content =
        startRule +
        pagesBuf.text() +
        "\r\n" +
        seam +
        "\r\n".repeat(Math.max(1, term.rows)) +
        streamBuf.replaceAll("\x1b[3J", ""); // belt-and-braces: never wipe the pages
      term.write(content, () => {
        // Anchor: the previously-top visible line (old buffer line 0 — we only prepend
        // at the very top) now sits `added` lines down; scroll back to it.
        const added = Math.max(0, term.buffer.active.length - before);
        if (added > 0) term.scrollToLine(added);
        // Live output that arrived DURING the rewrite was queued (writing it mid-rewrite
        // interleaves into the replayed content → torn frames / stray letters — the
        // "fractions of text" regression). Flush it after the anchor so ordering holds.
        rewriting = false;
        if (rewriteQueue.length) {
          for (const chunk of rewriteQueue) term.write(chunk);
          rewriteQueue.length = 0;
        }
      });
    };
    const maybeLoadOlder = () => {
      if (rewriting || id.startsWith("new-")) return; // placeholder: no transcript yet
      void loader.requestOlder(term.cols).then((page) => {
        if (sock !== sockRef.current) return; // superseded by a remount mid-fetch
        if (page?.ansi) prependPage(page.ansi);
      });
    };
    histRetryRef.current = () => {
      void loader.retry(term.cols).then((page) => {
        if (sock !== sockRef.current) return;
        if (page?.ansi) prependPage(page.ansi);
      });
    };
    // "Top" = the very first scrollback line of the NORMAL buffer is in view (an
    // alt-screen TUI has no scrollback to extend — never fetch there). The DOM
    // viewport's scrollTop is consulted alongside buffer.viewportY because xterm fires
    // `onScroll` only for scrollLines-driven scrolls (touch/API) — a desktop mouse-wheel
    // scroll moves the DOM `.xterm-viewport` without emitting it, so we listen to that
    // element's `scroll` event too (its ydisp sync can lag a frame; scrollTop doesn't).
    // #559: while a text selection is actively being made (desktop mouse-drag or mobile
    // long-press select-mode), pin the viewport so neither the browser/xterm drag-select edge
    // auto-scroll nor live-output follow drifts the view out from under the selection — the
    // reported "selecting text scrolls the terminal around." `beginSelectionPin` records the
    // scrollTop to hold; `onScrolled` snaps back to it, and the live-output `follow` is gated on
    // `!selectionActive`. Released on selection end (mouseup / exit select mode).
    let selectionActive = false;
    let selectionPinTop = 0;
    type ViewportAnchor = { viewportY: number; scrollTop: number };
    let readerAnchor: ViewportAnchor | null = null;
    const beginSelectionPin = () => {
      selectionActive = true;
      selectionPinTop = vpEl?.scrollTop ?? 0;
    };
    const endSelectionPin = () => {
      selectionActive = false;
    };
    const atTopNow = () => {
      const buf = term.buffer.active;
      if (buf.type !== "normal" || buf.baseY <= 0) return false;
      return buf.viewportY === 0 || (vpEl !== null && vpEl.scrollTop === 0);
    };
    // Auto-fetch arms only after a REAL scroll gesture (wheel / touch / keyboard paging).
    // During attach, xterm's layout fires viewport scroll events while scrollTop is still
    // transiently 0 — the detector saw "at top", fetched, and the rewrite anchored the
    // user near the TOP of history instead of the live tail (the "opens scrolled up"
    // regression). Programmatic scrolls must never arm it.
    let userScrolled = false;
    let sawOutput = false;
    // #533/#607/#616: fresh-launch input-ready detection. Readiness is "the boot output has gone
    // quiet"; the bracketed-paste enable (ESC[?2004h) only shortens the window it must stay quiet
    // for. It is NOT an instant ready — claude emits it before switching to the alternate screen
    // and clearing it, so a paste released on ?2004h gets wiped. See lib/bootReady.
    const bootGate = createBootReadyGate(markInputReady);
    const eventInTermArea = (target: EventTarget | null) => {
      const area = host.parentElement;
      return target instanceof Node && !!area?.contains(target);
    };
    const armHistory = () => {
      if (!sawOutput) return;
      userScrolled = true;
      initialTailLock = false;
      // #584: the user is now navigating, so the FAB is governed by their tracked scroll
      // (`appScrolledUp`) — drop the fresh-attach "tail unknown" flag so it doesn't linger.
      if (appTailUnknownRef.current) {
        setAppTailUnknown(false);
        appTailUnknownRef.current = false;
      }
    };
    const armOnWheel = (e: WheelEvent) => {
      if (!eventInTermArea(e.target)) return;
      armHistory();
      // App-consuming sessions (claude/opencode): xterm forwards this wheel to the agent, whose
      // scroll position we can't read — track net up-notches ourselves so the FAB knows the agent
      // has been scrolled up (#559). Trusted only: the jump-to-tail dispatches UNtrusted wheels,
      // which must not re-inflate the counter.
      if (e.isTrusted && appConsumesWheel(term)) {
        appScrollNotchesRef.current = Math.max(
          0,
          appScrollNotchesRef.current + (e.deltaY < 0 ? 1 : -1),
        );
        setAppScrolledUp(appScrollNotchesRef.current > 0);
      }
    };
    const armOnTouchMove = (e: TouchEvent) => {
      if (eventInTermArea(e.target)) armHistory();
    };
    const armOnKeydown = (e: KeyboardEvent) => {
      if (!eventInTermArea(e.target)) return;
      if (
        e.key === "PageUp" ||
        e.key === "PageDown" ||
        e.key === "Home" ||
        e.key === "End" ||
        e.key === "ArrowUp" ||
        e.key === "ArrowDown" ||
        (e.key === " " && e.shiftKey)
      ) {
        armHistory();
      }
    };
    const onScrolled = () => {
      // #559: hold the viewport still while a selection is in progress — drag-select edge
      // auto-scroll (or a stray follow) just tried to move it; snap straight back to where the
      // selection began so the highlighted text stays under the finger/cursor.
      if (selectionActive && vpEl && vpEl.scrollTop !== selectionPinTop) {
        vpEl.scrollTop = selectionPinTop;
        return;
      }
      if (
        sawOutput &&
        !selectionActive &&
        term.buffer.active.type === "normal" &&
        hasDomReaderOffset()
      ) {
        initialTailLock = false;
        readerAnchor = currentReaderAnchor();
      }
      updateAtBottom();
      const top = atTopNow();
      setAtTop(top);
      if (shouldPreserveReaderViewport()) readerAnchor = currentReaderAnchor();
      else if (computeAtBottom()) readerAnchor = null;
      if (top && userScrolled) maybeLoadOlder();
    };
    const shouldPreserveReaderViewport = () =>
      sawOutput &&
      !selectionActive &&
      !initialTailLock &&
      term.buffer.active.type === "normal" &&
      !computeAtBottom();
    const hasDomReaderOffset = () =>
      !!vpEl && vpEl.scrollTop > 0 && !computeDomAtBottom();
    const currentReaderAnchor = (): ViewportAnchor => {
      const rowHeight = vpEl
        ? Math.max(1, vpEl.clientHeight / Math.max(1, term.rows || 24))
        : 1;
      return {
        viewportY:
          vpEl && !computeDomAtBottom()
            ? Math.floor(vpEl.scrollTop / rowHeight)
            : term.buffer.active.viewportY,
        scrollTop: vpEl?.scrollTop ?? 0,
      };
    };
    const captureReaderAnchor = (): ViewportAnchor | null => {
      if (
        initialTailLock &&
        sawOutput &&
        !selectionActive &&
        term.buffer.active.type === "normal" &&
        hasDomReaderOffset()
      ) {
        initialTailLock = false;
      }
      if (shouldPreserveReaderViewport()) {
        readerAnchor = currentReaderAnchor();
        return readerAnchor;
      }
      if (
        selectionActive ||
        initialTailLock ||
        term.buffer.active.type !== "normal"
      )
        return null;
      return readerAnchor;
    };
    const restoreReaderAnchor = (anchor: ViewportAnchor | null) => {
      if (!anchor) return;
      readerAnchor = anchor;
      const buf = term.buffer.active;
      const line = Math.min(Math.max(anchor.viewportY, 0), buf.baseY);
      term.scrollToLine(line);
      if (vpEl) {
        const maxTop = Math.max(0, vpEl.scrollHeight - vpEl.clientHeight);
        vpEl.scrollTop = Math.min(anchor.scrollTop, maxTop);
      }
      updateAtBottom();
    };
    term.onScroll?.(onScrolled);
    vpEl?.addEventListener("scroll", onScrolled, { passive: true });
    // Document-level (capture): the coarse-pointer touch layer overlays the terminal
    // OUTSIDE host's subtree, so host-scoped listeners never see mobile gestures.
    document.addEventListener("wheel", armOnWheel, {
      passive: true,
      capture: true,
    });
    document.addEventListener("touchmove", armOnTouchMove, {
      passive: true,
      capture: true,
    });
    document.addEventListener("keydown", armOnKeydown, true);
    // #559 (desktop): a trusted left-button press may begin a drag-selection → pin the viewport
    // for the duration of the press (mobile arms the same pin from onLongPress below). isTrusted so
    // our own synthetic selection twin / click replay doesn't re-arm it; button 0 only.
    const onSelMouseDown = (e: MouseEvent) => {
      if (e.isTrusted && e.button === 0 && eventInTermArea(e.target))
        beginSelectionPin();
    };
    const onSelMouseUp = () => endSelectionPin();
    document.addEventListener("mousedown", onSelMouseDown, true);
    document.addEventListener("mouseup", onSelMouseUp, true);

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
        onOutput: (b) => {
          attachBytes += b.byteLength; // repaint-backstop signal: did this attach paint anything?
          sawOutput = true;
          if (!inputReadyRef.current) bootGate.note(b); // #533/#607/#616: fresh-launch compose gate
          const displayBytes = stripCodexLiveScrollbackErase(b);
          if (!displayBytes.byteLength) return;
          recordOutput(displayBytes); // feed the lazy-load rewrite buffer (#348 Phase 3)
          if (rewriting)
            rewriteQueue.push(displayBytes); // never interleave into a rewrite (#348)
          else {
            // Follow the live tail ONLY when the viewport is already sitting on it (measured
            // before the write) — so streaming output never yanks the reader out of scrollback,
            // no matter HOW they scrolled up (wheel, scrollbar drag, touch, keyboard). When they
            // are off the tail the scroll-to-bottom button (atBottom state) lets them jump back.
            // `initialTailLock` overrides this for the first attach replay only: a large raw
            // replay can otherwise leave xterm's DOM viewport parked above the final frame even
            // though the user never scrolled, presenting as an empty console (#407).
            // #559: never follow while a selection is in progress — new output must not yank the
            // view (and the highlighted text) away from under an active selection.
            const anchor = captureReaderAnchor();
            const follow =
              !anchor &&
              (initialTailLock || computeAtBottom()) &&
              !selectionActive;
            term.write(displayBytes, () => {
              if (follow) term.scrollToBottom();
              else restoreReaderAnchor(anchor);
              updateAtBottom(); // refresh the FAB even when not following — output grew the tail
            });
          }
        },
        onStatus: (s) => {
          setStatus(s);
          if (s.kind === "connected") {
            // The socket OPENED → the server received new=1 and launched. Only NOW stop sending the
            // launch params: if a first attempt is closed (watchdog / transient drop) BEFORE it
            // opens, the server never saw the launch, so the retry must relaunch — not attach to a
            // not-yet-existent session. (opencode `new-` placeholders keep new=1 until converged.)
            freshConsumed = true;
            onConnected();
            // Backstop only a TRUE fresh attach (consumed offset 0). A caught-up
            // reconnect (have == total) correctly receives no delta while the screen
            // is already painted — jiggling it would wipe a good frame (Hermes #374).
            if (sock.consumed === 0) armRepaintBackstop();
          }
        },
        onId: (sid) => onReconcileIdRef.current?.(sid),
        // {t:"hist"} (#348 / Hermes #365 r2): the transcript attach's EXACT turn boundary.
        // Seed the loader so the first lazy-load sends `before=<cursor>` — never the
        // width-dependent server guess. Arrives right after seq; the attach payload's
        // leading ESC[3J already purged pagesBuf + reset the loader (recordOutput above).
        onHist: (cursor) => loader.seed(cursor),
        onSeq: () => {
          attachReplayOpen = false;
          codexEraseCarry = new Uint8Array(0);
        },
        onRole: (r, h) => {
          setRole(r);
          // Owner → clear the banner. Secondary → show who's active (#434): we keep streaming
          // read-only behind the take-over banner instead of going blank. `h` names the active
          // viewer on the flag-on take-over path; the in-memory #184 path sends no holder, so
          // the banner falls back to generic "open in another tab" copy.
          setHolder(r === "owner" ? null : (h ?? { label: "" }));
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
      const readerAnchor = captureReaderAnchor();
      fit.fit();
      if (force) lastCols = lastRows = 0; // bypass the dedupe so the new pty is sized
      sendResize();
      if (readerAnchor)
        requestAnimationFrame(() => restoreReaderAnchor(readerAnchor));
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
        return;
      }
      // Neither an image nor text on the sync path. Deferred clipboard backends (observed:
      // Windows Chrome 149) can deliver an empty DataTransfer for a real image paste — try
      // the async clipboard before treating the paste as a no-op (#530), same fallback as
      // Compose. An actually-empty clipboard resolves to [] and stays a no-op.
      e.preventDefault();
      e.stopPropagation();
      void imageFilesFromAsyncClipboard().then((fallback) => {
        if (fallback.length) composeRef.current?.attachImages(fallback);
      });
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
    // Hard cap so a perpetually-jittering layout still attaches. Mobile address-bar /
    // keyboard animations regularly outlast 1.5s, and connecting mid-animation attaches
    // at an intermediate width — feeding the resize-vs-nudge coalescing blank (#349) and
    // dirtying the VT mirror. Coarse-pointer devices get double the budget; the QUIET
    // path still connects desktops and settled mobiles after ~130ms.
    const coarse =
      typeof window !== "undefined" &&
      window.matchMedia?.("(pointer: coarse)")?.matches;
    const MAX_FRAMES = coarse ? 180 : 90; // ~3s mobile / ~1.5s desktop
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
    const surfaceEl = touchLayer ?? host;
    // Tap → open a link under the finger, else (re)open the keyboard (#415). The overlay
    // sits above xterm, so xterm's own WebLinksAddon click never fires on touch; hit-test the
    // tapped cell against the buffer ourselves and open the same way the addon would.
    const focusKeyboardOnTap = () => {
      const ta = term.textarea;
      if (ta) {
        ta.blur();
        ta.focus();
      } else {
        term.focus();
      }
    };
    const onTap = (cx: number, cy: number) => {
      // The "jump to bottom" FAB paints ABOVE this capture overlay (z-index 7 vs 6), so per real
      // hit-testing a tap on it lands on the FAB itself — its onClick (scrollToTail, which also
      // cancels touch momentum) does the jump; #519's assumption that the overlay wins the tap was
      // wrong (see the #527 fix). This FAB-rect branch is a cheap defensive fallback for the rare
      // case a tap DOES reach the overlay within the FAB's rect: jump to the tail, not the keyboard.
      const fab = fabRef.current;
      if (fab) {
        const fr = fab.getBoundingClientRect();
        if (
          cx >= fr.left &&
          cx <= fr.right &&
          cy >= fr.top &&
          cy <= fr.bottom
        ) {
          // #559: app-consuming → forward to tail. #584: on a fresh attach the up-distance was never
          // tracked, so send a generous bounded burst (jumpToTail clamps at the agent's bottom).
          const notches = appTailUnknownRef.current
            ? Math.max(appScrollNotchesRef.current, (term.rows || 24) * 3)
            : appScrollNotchesRef.current;
          jumpToTailRef.current(notches);
          appScrollNotchesRef.current = 0;
          setAppScrolledUp(false);
          setAppTailUnknown(false);
          appTailUnknownRef.current = false;
          term.scrollToBottom();
          setAtBottom(true);
          return;
        }
      }
      const buf = term.buffer.active;
      const rect = surfaceEl.getBoundingClientRect();
      const cols = term.cols || 80;
      const rows = term.rows || 24;
      if (rect.width > 0 && rect.height > 0) {
        const col = Math.floor(((cx - rect.left) / rect.width) * cols);
        const vrow = Math.floor(((cy - rect.top) / rect.height) * rows);
        // #664: a long URL soft-wraps across buffer rows; hit-test the joined logical
        // line (as WebLinksAddon does on desktop), never a single row's fragment.
        const url = urlAtCell(buf, buf.viewportY + vrow, col, cols);
        if (url) {
          window.open(url, "_blank", "noopener,noreferrer");
          return;
        }
      }
      focusKeyboardOnTap(); // not on a link → behave as before
    };
    // Press-and-hold → selection mode (#415): drop the overlay so touches reach the rows, let
    // the OS select the DOM-rendered text (override xterm's user-select:none), and seed a word
    // selection at the finger so the native handles + Copy bubble appear at once. A later tap
    // with no selection restores scroll mode.
    let selecting = false;
    const exitSelectMode = () => {
      if (!selecting) return;
      selecting = false;
      endSelectionPin(); // #559: release the viewport pin when mobile select-mode ends
      term.element?.classList.remove(styles.selecting);
      if (touchLayer) touchLayer.style.pointerEvents = "";
      window.getSelection()?.removeAllRanges();
    };
    const onLongPress = (cx: number, cy: number) => {
      const el = term.element;
      if (!el) return;
      selecting = true;
      beginSelectionPin(); // #559: pin the viewport for the duration of mobile text selection
      el.classList.add(styles.selecting);
      if (touchLayer) touchLayer.style.pointerEvents = "none"; // before hit-test, so caret resolves to the rows
      try {
        const range = document.caretRangeFromPoint?.(cx, cy);
        const sel = window.getSelection();
        if (range && sel) {
          sel.removeAllRanges();
          sel.addRange(range);
          // Expand the caret to the word under the finger (native handles can refine).
          const s = sel as Selection & {
            modify?: (alter: string, dir: string, granularity: string) => void;
          };
          s.modify?.("move", "backward", "word");
          s.modify?.("extend", "forward", "word");
        }
      } catch {
        /* caretRangeFromPoint unsupported → overlay is still off; user can select manually */
      }
    };
    // While selecting, a lift that leaves no selection means "done" → back to scroll mode.
    const onDocTouchEnd = () => {
      if (selecting && !window.getSelection()?.toString()) exitSelectMode();
    };
    document.addEventListener("touchend", onDocTouchEnd, true);

    // Auto copy-on-select (#554): when a mouse selection settles, copy it to the clipboard and
    // flash the "Copied" toast — the operator-chosen flavor on top of #536 (plain-drag select +
    // Ctrl/⌘+C). Fires on `mouseup` (a real user gesture, so the async clipboard write is allowed
    // and the selection is already built up from the drag's mousemoves) — NOT `onSelectionChange`,
    // which fires mid-drag and outside a fresh gesture. Guards: our own synthetic events and the
    // touch select-mode (#415, its native Copy bubble owns mobile) are skipped; a plain click / an
    // empty or whitespace-only selection never clobbers the clipboard; and an unchanged selection is
    // not re-copied (so a click elsewhere while text stays selected is a no-op). `document` (not
    // `host`) so a drag that ends outside the terminal still copies.
    // #617: the toast FOLLOWS the clipboard write. `navigator.clipboard` is undefined on a
    // non-secure origin (plain-http LAN / dev), so the write silently no-opped while the toast
    // still said "Copied" — the UI lied. A failure now says so instead.
    let lastAutoCopied = "";
    let copiedHideTimer: number | undefined;
    let toastSeq = 0;
    const flashToast = (ok: boolean) => {
      const id = ++toastSeq;
      setCopied({ tick: id, ok });
      if (copiedHideTimer != null) clearTimeout(copiedHideTimer);
      copiedHideTimer = window.setTimeout(
        () => setCopied((c) => (c.tick === id ? { tick: 0, ok: true } : c)),
        ok ? COPIED_TOAST_MS : COPY_FAILED_TOAST_MS,
      );
    };
    const copyOnSelectSettle = (e: MouseEvent) => {
      if (!e.isTrusted || selecting || !term.hasSelection()) return;
      const text = term.getSelection();
      if (!text.trim() || text === lastAutoCopied) return;
      lastAutoCopied = text;
      const write = navigator.clipboard?.writeText(text);
      if (!write) {
        lastAutoCopied = ""; // nothing landed — let the same selection be retried
        flashToast(false);
        return;
      }
      void write.then(
        () => flashToast(true),
        () => {
          lastAutoCopied = "";
          flashToast(false);
        },
      );
    };
    document.addEventListener("mouseup", copyOnSelectSettle);

    const {
      detach: detachTouch,
      stopMomentum,
      jumpToTail,
    } = attachTouchScroll(surfaceEl, term, {
      onTap,
      onLongPress,
      // #559: one notch of touch scroll was forwarded to an app-consuming session — track how far
      // off the tail the agent is so the FAB shows and the jump-to-tail is sized (−1 = up).
      onAppScroll: (dir) => {
        appScrollNotchesRef.current = Math.max(
          0,
          appScrollNotchesRef.current + (dir < 0 ? 1 : -1),
        );
        setAppScrolledUp(appScrollNotchesRef.current > 0);
      },
    });
    stopMomentumRef.current = stopMomentum;
    jumpToTailRef.current = jumpToTail;

    // Attach once the grid is stable (see connectWhenStable) — NOT synchronously, or a still-
    // settling panel makes the post-connect resize wipe the transcript scroll-up (the race).
    connectWhenStable();
    return () => {
      cancelAnimationFrame(settleRaf);
      if (resizeTimer != null) clearTimeout(resizeTimer);
      bootGate.dispose();
      vv?.removeEventListener("resize", onVV);
      vpEl?.removeEventListener("scroll", onScrolled);
      clearJiggle();
      jiggleRef.current = () => {}; // stale REPAINT click must not resize a disposed socket (#485)
      document.removeEventListener("wheel", armOnWheel, true);
      document.removeEventListener("touchmove", armOnTouchMove, true);
      document.removeEventListener("keydown", armOnKeydown, true);
      document.removeEventListener("mousedown", onSelMouseDown, true); // #559 selection pin
      document.removeEventListener("mouseup", onSelMouseUp, true);
      host.removeEventListener("mousedown", onTermMouseDown, true);
      document.removeEventListener("mousemove", onGestureMove, true);
      document.removeEventListener("mouseup", onGestureUp, true);
      host.removeEventListener("paste", onHostPaste, true);
      detachTouch();
      stopMomentumRef.current = () => {};
      jumpToTailRef.current = () => {}; // #559: a stale FAB click must not wheel a disposed socket
      document.removeEventListener("touchend", onDocTouchEnd, true);
      document.removeEventListener("mouseup", copyOnSelectSettle);
      if (copiedHideTimer != null) clearTimeout(copiedHideTimer);
      exitSelectMode();
      touchLayer?.remove();
      ro.disconnect();
      sock.close();
      sockRef.current = null;
      termRef.current = null;
      fitRef.current = null;
      writeParsedSub.dispose();
      term.dispose();
    };
    // Identity-only deps: this socket lives and dies with the terminal's `key` (engine:id).
    // `fresh` is intentionally excluded — it's read once via freshRef so self-convergence
    // (which clears route state) can't tear down + relaunch the live terminal. See freshRef.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engine, id, takeoverEpoch]);

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
  // #284: use the server-resolved display title only (manual rename → AI title → meaningful
  // first message, else ""). Never fall back to the RAW first message, or a stray "a" / "."
  // leaks into the panel header — drop straight to the short id.
  const title = row?.title || `${id.slice(0, 8)}…`;
  // Header meta run (#744): the same facts the sidebar row carries — project and how stale the
  // session is. A folder ref's `name` is the FULL cwd by server contract (projects.resolve), so
  // clients shorten it themselves; an adopted project keeps its entity name + colour dot.
  const projectLabel = row
    ? row.project.kind === "project"
      ? row.project.name
      : shortCwd(row.project.name)
    : "";
  const projectStyle =
    row?.project.kind === "project"
      ? ({
          "--proj": row.project.color || projectColor(row.project.id),
        } as CSSProperties)
      : undefined;
  // Session-brief modal (#481): the recap icon in the header opens it; the trigger element is
  // captured at click time so focus returns to it on close (no ref read during render).
  const [recapOpen, setRecapOpen] = useState(false);
  const [recapTrigger, setRecapTrigger] = useState<HTMLElement | null>(null);
  // Hand-off modal (#597): same trigger-capture pattern as the recap. Hidden for the
  // non-agent shell engine and for a still-unreconciled placeholder id (no transcript
  // under that id yet — the source of a handoff is its real, scanned identity).
  const [handoffOpen, setHandoffOpen] = useState(false);
  const [handoffTrigger, setHandoffTrigger] = useState<HTMLElement | null>(
    null,
  );
  const canHandoff = engine !== "shell" && !id.startsWith("new-");
  const scrollToTail = useCallback(() => {
    // Kill any in-flight touch-momentum glide FIRST: without this, a tap on the FAB while the
    // scroll-up fling is still decaying scrolls to the tail for one frame and is then dragged
    // straight back up by the leftover velocity — the reported "jump-to-bottom does nothing on
    // phones" (#519 follow-up). The fling lives in attachTouchScroll and its own stopFling only
    // runs when the overlay receives a touch; the FAB sits above the overlay and takes the tap.
    stopMomentumRef.current();
    // App-consuming sessions (claude/opencode): the agent owns the scroll, so scrollToBottom() is a
    // no-op — forward the tracked up-distance back down as wheel notches so the agent returns to its
    // live tail (#559). No-op for a scrollback session (codex), where scrollToBottom() does the work.
    // #584: on a fresh attach we never tracked how far up the agent opened (appScrollNotchesRef is 0),
    // so forward a generous, bounded burst (a few screenfuls; jumpToTail clamps at the agent's bottom
    // and caps the count) — otherwise a 0-notch jump would move nothing.
    const jumpNotches = appTailUnknownRef.current
      ? Math.max(appScrollNotchesRef.current, (termRef.current?.rows ?? 24) * 3)
      : appScrollNotchesRef.current;
    jumpToTailRef.current(jumpNotches);
    appScrollNotchesRef.current = 0;
    setAppScrolledUp(false);
    // #584: the user asked for the tail — resolve the fresh-attach "unknown" state so the FAB clears.
    setAppTailUnknown(false);
    appTailUnknownRef.current = false;
    termRef.current?.scrollToBottom();
    setAtBottom(true);
  }, []);
  // Manual repaint (#485): force the agent to redraw its current frame via the published
  // rows−1→rows nudge — recovers a mid-session blank/fragment (a winch-repaint TUI that cleared
  // its viewport and went quiet) WITHOUT killing the process. Non-destructive, unlike RESTART.
  const repaint = useCallback(() => {
    jiggleRef.current();
  }, []);
  // Auto-repaint when the tab/window is (re)surfaced (#503): a backgrounded tab comes back to a
  // stale or blank frame (mobile browsers freeze the canvas, and a winch-repaint TUI that cleared
  // its viewport stays quiet). On resurfacing — owner only — fire the same non-destructive repaint
  // nudge as the button. Three distinct signals, because none subsumes the others:
  //   - visibilitychange: tab switch / minimize / mobile background.
  //   - window 'focus': alt-tab BACK from another app/window while the tab stayed visibilityState
  //     "visible" — visibilitychange never fires for that, so a desktop refocus was previously
  //     missed (the "I clicked back and it's stale" case).
  //   - pageshow: bfcache restore (mobile back/forward) replays a frozen canvas.
  // The visibility guard keeps a background 'focus' from spending a wasted SIGWINCH, and a short
  // coalesce window collapses the visibilitychange+focus double-fire that a tab-return emits into
  // one nudge (no double flicker). The nudge is a no-op while disconnected (stale jiggleRef), so a
  // reconnect-on-return still works.
  useEffect(() => {
    if (role !== "owner") return;
    let lastNudge = 0;
    const nudgeIfVisible = () => {
      if (document.visibilityState !== "visible") return;
      const now = Date.now();
      if (now - lastNudge < 500) return; // collapse the visibilitychange+focus double-fire
      lastNudge = now;
      repaint();
    };
    document.addEventListener("visibilitychange", nudgeIfVisible);
    window.addEventListener("focus", nudgeIfVisible);
    window.addEventListener("pageshow", nudgeIfVisible);
    return () => {
      document.removeEventListener("visibilitychange", nudgeIfVisible);
      window.removeEventListener("focus", nudgeIfVisible);
      window.removeEventListener("pageshow", nudgeIfVisible);
    };
  }, [role, repaint]);
  const takeover = useCallback(() => {
    // Arm the one-shot force flag, then bump the epoch: the effect reconnects and the fresh
    // connect carries ?force=1, demoting the prior owner on the server (#184).
    forceNextConnectRef.current = true;
    setTakeoverEpoch((n) => n + 1);
  }, []);
  // Order: Files leads (the new primary affordance); Repaint stays ahead of the fold because
  // burying the recovery control when the screen is blank would be the wrong trade.
  // An array LITERAL with conditional entries, not an imperative `push` — mutating an array during
  // render made the compiler treat the captured callbacks (which read refs) as render-time ref
  // access. The compiler memoizes this for us, so no manual useMemo either.
  const headActions: HeadAction[] = [
    // Files leads: it is the new primary affordance.
    ...(onToggleFiles ? [{
      id: "files",
      label: "Files",
      aria: "Browse session files",
      title: filesDisabledReason ?? "Browse this session's files and folders",
      icon: <PanelRight size={13} aria-hidden="true" />,
      active: filesOpen,
      disabled: Boolean(filesDisabledReason),
      run: (trigger?: HTMLElement | null) => onToggleFiles(trigger),
    }] : []),
    // Repaint stays ahead of the fold: burying the recovery control when the screen is blank
    // would be the wrong trade.
    ...(role === "owner" ? [{
      id: "repaint",
      label: "Repaint",
      aria: "Repaint screen",
      title:
        "Repaint the screen: nudge the agent to redraw its current frame (recovers a blank/fragment) — does not restart the agent",
      icon: <RotateCw size={13} aria-hidden="true" />,
      disabled: status.kind !== "connected",
      // An inline arrow, not `run: repaint`: handing the ref-reading callback across as a value
      // makes the compiler treat it as ref access during render. Same shape Compose uses for
      // KeyBar's actions.
      run: () => repaint(),
    }] : []),
    {
      id: "recap",
      label: "Recap",
      aria: "Open session brief",
      title: "Session brief: full title, summary, and a chronological recap of this session",
      icon: <ScrollText size={13} aria-hidden="true" />,
      run: (trigger?: HTMLElement | null) => {
        setRecapTrigger(trigger ?? (document.activeElement as HTMLElement | null));
        setRecapOpen(true);
      },
    },
    ...(canHandoff ? [{
      id: "handoff",
      label: "Hand off",
      aria: "Hand off session to another engine",
      title: "Hand off: start a new session in another engine, seeded with this session's context",
      icon: <ArrowLeftRight size={13} aria-hidden="true" />,
      run: (trigger?: HTMLElement | null) => {
        setHandoffTrigger(trigger ?? (document.activeElement as HTMLElement | null));
        setHandoffOpen(true);
      },
    }] : []),
  ];

  return (
    <div className={styles.wrap}>
      {/* Panel header (#211 4c, re-cut in #744): a HUD meta run — semantic LED, engine box,
          project, relative update time — mirroring what the sidebar row shows for this session,
          then the action buttons. The session title is deliberately absent: it is the sidebar's
          job and the session brief's, and the 26px bar reads better carrying facts the sidebar
          can't repeat next to the live pane (which project, how stale). */}
      <div className={styles.panelHead}>
        <span className={styles.headLeft}>
          <span
            className={`${styles.headLed} hud-led ${head.led}`}
            role="img"
            aria-label={`status: ${head.label.toLowerCase()}`}
            title={head.label}
          />
          <span className={styles.headEng} title={engineName(engine)}>
            {engineBadge(engine)}
          </span>
          {row && (
            <span className={styles.headMeta}>
              {projectLabel && (
                <span
                  className={styles.headProject}
                  style={projectStyle}
                  title={projectLabel}
                >
                  {row.project.kind === "project" && (
                    <span
                      className={styles.headProjectDot}
                      aria-hidden="true"
                    />
                  )}
                  {projectLabel}
                </span>
              )}
              {/* Drops first as the pane narrows (see .headUpdated) — it carries the separator
                  with it, so the project never trails a dangling "·". */}
              <span className={styles.headUpdated}>
                {projectLabel ? " · " : ""}
                {relTime(row.last_mtime)}
              </span>
            </span>
          )}
        </span>
        {/* Actions with measured overflow (#783). A fourth labelled button breaks the header's
            own measured contract (see Terminal.module.css), so trailing actions fold into a "…"
            menu that still carries full labels — the KeyBar idiom, not an icon-only shrink. */}
        <HeadActions
          className={styles.headActions}
          btnClassName={styles.restartBtn}
          labelClassName={styles.headActionLabel}
          actions={headActions}
        />
      </div>
      {recapOpen && (
        <SessionRecapModal
          sessionId={`${engine}:${id}`}
          engine={engine}
          title={title}
          project={row?.project}
          lastMtime={row?.last_mtime}
          // The SESSION's status (#744) — the brief resolves it from the row with the SAME
          // resolver the sidebar dot uses, not this pane's socket state. The header LED above
          // answers a different question (is THIS browser attached), so the two are deliberately
          // different signals.
          statusRow={row}
          summary={row?.ai_summary}
          recap={row?.ai_recap}
          interventionRequired={row?.intervention_required}
          interventionReason={row?.intervention_reason}
          reviewedAt={row?.reviewed_at}
          reviewExcluded={row?.review_excluded}
          onClose={() => setRecapOpen(false)}
          returnFocusTo={recapTrigger}
        />
      )}
      {handoffOpen && (
        <HandoffModal
          sessionId={`${engine}:${id}`}
          engine={engine}
          title={title}
          onClose={() => setHandoffOpen(false)}
          returnFocusTo={handoffTrigger}
        />
      )}
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
        {/* Auto copy-on-select confirmation (#554): keyed on the tick so each copy restarts the
            fade. role/aria-live announce it; the JS timer unmounts it after COPIED_TOAST_MS. */}
        {copied.tick !== 0 && (
          <div
            key={copied.tick}
            className={
              copied.ok
                ? styles.copiedToast
                : `${styles.copiedToast} ${styles.copyFailed}`
            }
            role="status"
            aria-live="polite"
            data-copied-toast=""
            data-copy-ok={copied.ok ? "" : undefined}
            data-copy-failed={copied.ok ? undefined : ""}
          >
            {copied.ok ? "Copied" : "Copy needs a secure origin"}
          </div>
        )}
        {/* Scroll-up lazy-load pills (#348 Phase 3, per the issue mockup): absolutely
            positioned overlays at the terminal top — never buffer rows, so a page
            prepend can't shift them. */}
        {histState === "loading" && (
          <div className={styles.histPillRow}>
            <span
              className={styles.histPill}
              role="status"
              data-hist-pill="loading"
            >
              <span className={styles.histSpin} aria-hidden="true" />
              loading older history…
            </span>
          </div>
        )}
        {histState === "end" && atTop && (
          <div className={styles.histPillRow}>
            <span
              className={`${styles.histPill} ${styles.histPillMuted}`}
              role="status"
              data-hist-pill="end"
            >
              — start of history —
            </span>
          </div>
        )}
        {/* Local depth cap (Hermes #365 r2): the last fetched page couldn't be retained.
            Takes the start-of-history pill's slot; no further auto-fetches fire. */}
        {histState === "capped" && atTop && (
          <div className={styles.histPillRow}>
            <span
              className={`${styles.histPill} ${styles.histPillMuted}`}
              role="status"
              data-hist-pill="capped"
            >
              — older history beyond local cap —
            </span>
          </div>
        )}
        {histState === "error" && (
          <div className={styles.histPillRow}>
            <button
              type="button"
              className={`${styles.histPill} ${styles.histPillError}`}
              data-hist-pill="error"
              aria-label="Couldn't load older history — tap to retry"
              onClick={() => histRetryRef.current()}
            >
              couldn&apos;t load older history —{" "}
              <span className={styles.histRetry}>tap to retry ↻</span>
            </button>
          </div>
        )}
        {/* Scroll-to-bottom button (#187, generalised): shown on EVERY pointer type whenever the
            viewport is off the live tail, so desktop users who scrolled up into history have a
            one-click jump back to the tail (and follow resumes once they are at the bottom).
            `appScrolledUp` extends it to mouse-tracking sessions (claude/opencode), whose scroll the
            agent owns so xterm's `atBottom` stays true — there the tap forwards a jump-to-tail (#559).
            `appTailUnknown` extends it further to a FRESH attach of such a session, which opens off
            its live tail with nothing yet scrolled — so the user has a one-tap way back (#584). */}
        {(!atBottom || appScrolledUp || appTailUnknown) && (
          <button
            ref={fabRef}
            type="button"
            className={styles.scrollFab}
            aria-label="Scroll to bottom"
            title="Scroll to bottom"
            onClick={scrollToTail}
          >
            <ArrowDown size={20} />
          </button>
        )}
        {/* Read-only take-over banner (#184/#293/#434): a secondary viewer streams read-only
            (never blank) behind this banner. "Take over" force-reconnects to promote this tab. */}
        {role === "secondary" && (
          <div className={styles.secondaryBanner} role="status">
            <span>
              {holder?.label?.trim()
                ? `Read-only — "${holder.label.trim()}" is the active viewer. Your input is disabled.`
                : "This session is open in another tab. You're viewing in read-only mode."}
            </span>
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
      </div>
      {/* Action/compose bar everywhere; default state per the compose pref (#254), falling back
          to the device heuristic — expanded on touch, collapsed-to-the-bar on desktop. */}
      {/* #477: persist the compose draft server-side per session. A not-yet-real
          `new-…` placeholder has no metadata key (out of scope) → drafts disabled. */}
      <Compose
        ref={composeRef}
        sendInput={sendInput}
        connEpoch={connEpoch}
        waitInputReady={waitInputReady}
        defaultOpen={composeDefaultOpen}
        sessionId={id.startsWith("new-") ? null : `${engine}:${id}`}
      />
    </div>
  );
}
