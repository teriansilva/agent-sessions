import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { Terminal as Xterm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import {
  AArrowDown,
  AArrowUp,
  ArrowDown,
  ArrowLeftRight,
  PanelRight,
  RotateCw,
  ScrollText,
  SquareDashedBottom,
  Crosshair,
  Share2,
} from "lucide-react";
import {
  type Ref,
  type RefObject,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { api } from "../../lib/api";
import { createBootReadyGate } from "../../lib/bootReady";
import { getBrowserFp, getTabId } from "../../lib/browserFp";
import { getDeviceLabel } from "../../lib/deviceLabel";
import { HistoryLoader, type HistoryState } from "../../lib/historyLoader";
import { PagesBuffer, foldWipe } from "../../lib/pagesBuffer";
import { createScrollEraseStripper } from "../../lib/scrollErase";
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
import { useSessionRow } from "../../app/useSessionRow";
import { useIsMobile } from "../../lib/useIsMobile";
import { isNewSessionPlaceholder, useSessionsStore } from "../../app/sessionsStore";
import { AdoptToMissionModal } from "../sessions/AdoptToMissionModal";
import {
  TermSocket,
  type TermGateHolder,
  type TermRole,
  type TermStatus,
} from "../../lib/termSocket";
import { type FreshSession, termWsUrl } from "../../lib/termUrl";
import { appConsumesWheel, attachTouchScroll } from "../../lib/touchScroll";
import { useAccent } from "../../theme/accentStore";
import {
  stepTermFontSize,
  TERM_FONT_SIZE_MAX,
  TERM_FONT_SIZE_MIN,
} from "../../theme/termSize";
import { useTermFont } from "../../theme/termFontStore";
import { useTermSize } from "../../theme/termSizeStore";
import { xtermTheme } from "../../theme/themes";
import { useTheme } from "../../theme/themeStore";
import { Compose, type ComposeHandle, type TemplateDraft } from "./Compose";
import { draftSessionKey } from "../../lib/draftSessionKey";
import { HandoffModal } from "./HandoffModal";
import { HeadActions, type HeadAction } from "./HeadActions";
import { HeadFacts } from "./HeadFacts";
import { SessionRecapModal } from "./SessionRecapModal";
import styles from "./Terminal.module.css";
import { missionLink } from "../../lib/missionLink";
import { shareLink } from "../../lib/shareLink";
import { openTerminalLink } from "../../lib/terminalLink";
import { isAgent, useEngineRoster } from "../../app/engineRoster";

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

/** The live terminal: an xterm pane bridged to /ws/term/{engine}:{id} via TermSocket.
 *  Output is written verbatim; keystrokes and resize go back as JSON; reconnect +
 *  delta-resume (never-blank) is owned by TermSocket. Remount per session via `key`. */
/** What a parent can ask the terminal pane to do (#792).
 *
 *  SessionView renders the file panel beside this pane but never sees Compose — `composeRef` and
 *  the `<Compose>` instance both live in here. So the panel's "send this path" travels
 *  SessionView → Terminal → Compose rather than reaching Compose directly. */
export interface TerminalHandle {
  /** Splice a path token into the compose draft at the caret. Never sends. */
  insertToken: (token: string) => void;
  /** Open the composer's template picker, optionally on one template (#905 P3). Never sends. */
  openTemplates: (templateId?: string) => void;
}

export function Terminal({
  engine,
  id,
  rowKey,
  fresh,
  onReconcileId,
  onToMap,
  onSaveAsTemplate,
  onOpenGallery,
  filesOpen,
  onToggleFiles,
  filesDisabledReason,
  onRole,
  suppressHead,
  headActionsSlot,
  headOverflowRef,
  headReservePx,
  headBarRef,
  onTermStatus,
  ref,
}: {
  engine: string;
  id: string;
  /** The id the URL has settled on, when it differs from this terminal's frozen identity —
   *  i.e. after the opencode/codex placeholder→real converge (#127/#315).
   *
   *  `engine`/`id` stay frozen on the placeholder ON PURPOSE: re-keying them would tear down the
   *  live socket. But the session ROW only ever exists under the real id, so resolving header
   *  metadata from the frozen key alone left the header, Recap and Hand off nameless for the rest
   *  of a converged session's life — the file panel recovered because `SessionView` already
   *  looked under both. This carries the real key for the row lookup ONLY; nothing here feeds the
   *  terminal's identity, so the frozen socket is untouched (#867). */
  rowKey?: string;
  fresh?: FreshSession;
  /** "Save as template" from the composer or its history (#905 P3) — straight through to Compose. */
  onSaveAsTemplate?: (draft: TemplateDraft) => void;
  /** The picker's gallery links (#908 round 4) — straight through to Compose. */
  onOpenGallery?: (to: string) => void;
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
  /** Put this session on the map as a window and go there (#936) — the exact inverse of a
   *  window's ⤢. Optional, and passed ONLY by the full-screen route: a session already living
   *  in a window must not offer to window itself. */
  onToMap?: () => void;
  /** The #184/#293 role verdict, published for a host that frames this pane (#208: the map's
   *  window chrome shows READ-ONLY). Read-only signal — the pane keeps rendering its own
   *  banner and Take over button; nothing about ownership moves out here. */
  onRole?: (role: TermRole) => void;
  /** Publish the pane's socket status to a host that renders the LED for it (#1109: the window
   *  chrome carries the facts run, and the LED is this pane's attachment state, which only the
   *  pane knows). Named `onTermStatus`, NOT `onStatus`: TermSocket's own config option inside
   *  the socket effect is called `onStatus`, and a prop by that name shadowed it badly enough
   *  that the React compiler refused to optimize the component. Ref-held like `onRole`, so an
   *  unstable closure never re-keys the socket. */
  onTermStatus?: (status: TermStatus) => void;
  /** Suppress this pane's own `panelHead` (#1109) — a map window's chrome bar carries the
   *  header instead, so a window never shows two stacked bars. The full-screen pane NEVER sets
   *  it. The modals the head actions open (recap, hand off, adopt) stay mounted here: their
   *  triggers simply render wherever `headActionsSlot` points. */
  suppressHead?: boolean;
  /** With `suppressHead`: the DOM node the action chips portal into — a slot the window's
   *  chrome bar provides. Absent → the chips render nowhere (the one frame before the chrome's
   *  slot element exists; the window opens with a socket connecting anyway). */
  headActionsSlot?: HTMLElement | null;
  /** With `suppressHead`: the host's ref the chips' measured fold publishes through
   *  (`HeadActions` `foldInto: "external"`), so the chrome's single ⋯ menu carries whatever
   *  overflowed, read at menu-open time. */
  headOverflowRef?: { current: HeadAction[] };
  /** With `suppressHead`: the bar width the chips must leave for everything else on the
   *  chrome bar (facts run, title, window buttons). See `HeadActions.reservePx`. */
  headReservePx?: number;
  /** With `suppressHead`: the chrome BAR the chips' fold budgets against — the chips portal
   *  into a slot, so the default parent measurement would measure the slot, not the bar. */
  headBarRef?: RefObject<HTMLElement | null>;
  /** React 19 passes `ref` as an ordinary prop; the handle is `TerminalHandle`. */
  ref?: Ref<TerminalHandle>;
}) {
  // Re-render when the engine roster lands or changes (#853 P4): this renders agent names,
  // badges or colours, which come from the roster, not from a client-side list.
  useEngineRoster();
  const hostRef = useRef<HTMLDivElement>(null);
  const fabRef = useRef<HTMLButtonElement>(null);
  // Halts an in-flight touch-momentum glide (set by attachTouchScroll). The FAB's jump-to-
  // tail calls it first, or leftover fling velocity from the scroll-up drag drags the view
  // straight back off the tail (#519 follow-up — the FAB paints above the touch overlay and
  // takes the tap itself, so the overlay's own stopFling never runs for it).
  const stopMomentumRef = useRef<() => void>(() => {});
  const sockRef = useRef<TermSocket | null>(null);
  const composeRef = useRef<ComposeHandle>(null);
  useImperativeHandle(ref, () => ({
    // Straight pass-through: the panel's token is Compose's business, and Terminal only owns the
    // ref that reaches it.
    insertToken: (token: string) => composeRef.current?.insertToken(token),
    openTemplates: (templateId?: string) => composeRef.current?.openTemplates(templateId),
  }));
  const termRef = useRef<Xterm | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  // Manual repaint (#485): the live socket effect publishes its rows−1→rows nudge here so the
  // owner-only REPAINT header button can force a winch-repaint agent to redraw its current frame
  // WITHOUT killing the process (unlike RESTART). Reset to a no-op on teardown so a stale click
  // can't resize a disposed socket.
  const jiggleRef = useRef<() => void>(() => {});
  // #859: the debounced refit, published by the live socket effect for the SAME reason
  // jiggleRef is — the size effect must reach the existing debounce without joining that
  // effect's identity-only dep array, which would tear down and rebuild xterm and the
  // WebSocket on every tap of the stepper. Reset to a no-op on teardown so a late size
  // change can't refit a disposed terminal.
  const refitSoonRef = useRef<() => void>(() => {});
  const { theme } = useTheme();
  const { accent } = useAccent();
  const { size: termFontSize, setSize: setTermFontSize } = useTermSize();
  // The size the socket effect reads when CONSTRUCTING xterm. A ref, not a dep: a fresh
  // terminal must open at the current size, but a size *change* must never re-key the
  // socket. Declared before that effect so this sync runs first in the same commit.
  const termFontSizeRef = useRef(termFontSize);
  useEffect(() => {
    termFontSizeRef.current = termFontSize;
  });
  const { family: termFontFamily } = useTermFont();
  // Same ref treatment as the size above, and for the same reason (#866): a fresh terminal
  // must open in the current face, but changing the face must never re-key the socket —
  // that would tear down xterm and the WebSocket on every card tap.
  const termFontFamilyRef = useRef(termFontFamily);
  useEffect(() => {
    termFontFamilyRef.current = termFontFamily;
  });
  // Live column count for the quick-zoom readout (#859). Fed by xterm's own resize event, so
  // it tracks a rotation or a sidebar toggle, not just a stepper tap.
  const [cols, setCols] = useState(0);
  // The shell's own ≤800px breakpoint (#948 P6): the header becomes one Actions menu there.
  const isMobile = useIsMobile();
  // "Adopt to mission" / "Open mission" (#948 P5) — the header half of session-side adoption.
  const [adoptOpen, setAdoptOpen] = useState(false);
  const [adoptTrigger, setAdoptTrigger] = useState<HTMLElement | null>(null);
  // The header the dialog was opened from. Adoption replaces the Adopt chip with Open mission, and
  // a longer label can fold it into "…", so focus is resolved against the header, not one node.
  const adoptHeadRef = useRef<HTMLElement | null>(null);
  const resolveAdoptReturn = useCallback(() => {
    const head = adoptHeadRef.current;
    return (
      head?.querySelector<HTMLElement>('[data-head-action="mission"]') ??
      head?.querySelector<HTMLElement>('button[aria-haspopup="menu"]') ??
      null
    );
  }, []);
  const navigate = useNavigate();
  const { remember } = useSessionsStore();
  // The row for THIS session (#232, re-sourced in #867) — project, title, update time, review
  // fields. It comes from the sidebar's loaded page when that has it and from a per-session
  // lookup when it doesn't, so a deep-linked / archived / list-hidden session names itself
  // instead of showing a bare LED. Falls back to a short id while nothing has resolved yet.
  // Read-only: it never re-keys the socket.
  const row = useSessionRow(`${engine}:${id}`, rowKey);
  // TWO identities, and conflating them is the #127 converge bug in its second form (#867
  // review round 3). `${engine}:${id}` is the TRANSPORT identity — frozen on the placeholder for
  // life so the socket, lock and ring keep pointing at the master we launched. `actionKey` is
  // what the SERVER should act on, which after `onReconcileId` is the real id the engine minted:
  // the placeholder is rejected by `canonical_key`, so a "Review now" or a handoff prepare aimed
  // at it silently targets a session that does not exist. Transport stays frozen; actions follow
  // the URL.
  const actionKey = rowKey || `${engine}:${id}`;
  // …and the same distinction decides whether Hand off is even offered. Gating on the frozen
  // `id` left it hidden for the entire life of a converged opencode/codex session, because that
  // id keeps its `new-` prefix forever.
  const actionNative = actionKey.slice(actionKey.indexOf(":") + 1);
  // #1109: the socket status is also PUBLISHED to a host that renders the LED for this pane
  // (the window chrome) — through the SAME write, not a second effect: the React compiler
  // refuses the component outright when `status` is both an effect dep and a HeadFacts prop,
  // and one write is the honest shape anyway. The publish itself is the `setStatus` wrapper
  // by the onRole idiom below.
  const [status, setStatusRaw] = useState<TermStatus>({ kind: "connecting" });
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
  // #1108: the reader's saved scrollback position lives inside the socket effect; the ↓ FAB
  // (outside it) marks the tail as requested before it moves the buffer (see scrollToTail).
  const requestTailRef = useRef<() => void>(() => {});
  // Auto copy-on-select "Copied" toast (#554): 0 = hidden, else a monotonic tick used as the
  // element key so each copy restarts the fade animation. Set by the mouseup copy-on-settle handler.
  // #554 copy-on-select toast; `ok` false = the clipboard write failed (insecure origin) (#617).
  const [copied, setCopied] = useState<{ tick: number; ok: boolean }>({
    tick: 0,
    ok: true,
  });
  // Share link (#1232): what the clipboard fallback did — the share sheet reports itself.
  const [linkToast, setLinkToast] = useState<{ tick: number; ok: boolean }>({
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
  // #208: the host's role callback, held in a ref for the same reason `jiggleRef` exists — the
  // live socket effect reads it without joining its identity-only dep array, so a parent that
  // re-renders with a fresh closure never re-keys the socket.
  const onRoleRef = useRef(onRole);
  useEffect(() => {
    onRoleRef.current = onRole;
  });
  // Same ref idiom for `onTermStatus` (#1109): a host that renders the LED for this pane reads
  // it through a ref, so an unstable closure cannot re-key the socket. The PUBLISH rides the
  // status write itself (`setStatus` below), not a second effect on `status`: the React
  // compiler refuses the component outright when `status` is both an effect dependency and a
  // HeadFacts prop, and one write is the honest shape anyway — the host's own initial state
  // matches this pane's opening state, so "connecting" needs no publish.
  const onTermStatusRef = useRef(onTermStatus);
  useEffect(() => {
    onTermStatusRef.current = onTermStatus;
  });
  const setStatus = useCallback((s: TermStatus) => {
    setStatusRaw(s);
    onTermStatusRef.current?.(s);
  }, []);
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
    const term = new Xterm({
      cursorBlink: true,
      // #859: the size is its own axis now (theme/termSize.ts), not a theme field.
      fontSize: termFontSizeRef.current,
      // Lines of live (dtach-stream) scroll-up the browser retains while connected — distinct
      // from the rendered transcript. 50k keeps a deep session in reach; xterm stores lines
      // compactly so the memory cost is modest. Pairs with the server ring (_MAX_BUF) that backs
      // reconnect replay.
      scrollback: 50000,
      // #866: the FACE is its own axis too (theme/termFont.ts), no longer a theme field.
      fontFamily: termFontFamilyRef.current,
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
    // Make URLs in agent output clickable (#158). A BattleLab link on this origin opens here, the
    // way an outside link would; anything else opens in a new tab with no opener and no Referer
    // (#1232, see lib/terminalLink).
    term.loadAddon(
      new WebLinksAddon((e, uri) => {
        openTerminalLink(uri, e.metaKey || e.ctrlKey);
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
    // #1108: the ↓ jump asked for the tail and xterm's DOM viewport has not caught up yet. While
    // set, a buffer/DOM disagreement is that lag, never a reader off the tail: no anchor is
    // recorded and output follows. Cleared once the DOM reports the tail, the buffer leaves it,
    // or the operator scrolls.
    let tailRequested = false;
    const settleTailRequest = () => {
      if (!tailRequested) return;
      const buf = term.buffer.active;
      if (buf.baseY - buf.viewportY > SCROLL_DEAD_ZONE || computeDomAtBottom())
        tailRequested = false;
    };
    const computeAtBottom = () => {
      // #1108: a requested tail the DOM has not reached yet is still the tail.
      settleTailRequest();
      if (tailRequested) return true;
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
    // Live-stream wipe strip (#600 codex, #1038 kimi) — lib-extracted with its across-chunk
    // carry. The server applies the identical filter to the dtach stream before ring/mirror/ws;
    // this copy protects the post-`seq` stream (and the rewrite replay below) even on sessions
    // whose ring predates the server fix. Attach replays keep their wipes (pre-attach junk).
    const stripLiveScrollbackErase = createScrollEraseStripper();
    // Blank-attach repaint backstop (#349 follow-up, operator report): some idle
    // sessions paint fragments or nothing on selection — the server-side nudge can be
    // coalesced/missed, and only a REAL geometry change reliably makes winch-repaint
    // agents redraw. When an attach delivers (almost) no bytes OR the visible xterm
    // rows are still blank after a large replay (#407), the CLIENT jiggles rows−1 →
    // rows. Rows-only on purpose: a width change would reset the scrollback
    // ring / dirty the VT mirror. The client owns the resize channel, so nothing can
    // interleave inside its pair (unlike the server nudge racing the connect resize),
    // and the spacing exceeds the agents' resize debounce → two distinct repaints.
    //
    // ALT-SCREEN SESSIONS REPAINT ON EVERY CONNECT (operator, 2026-09-28: "when I access a session I
    // usually need to press repaint"). The heuristics below can only see EMPTY rows, so a full
    // frame replayed at a stale geometry passed them, and a reconnect (a phone waking) never
    // armed them at all. Their caution is about the NORMAL buffer: there a repaint's clear wipes
    // the injected scroll-up from view (#300) and a reconnect's frame is already good (Hermes
    // #374). An agent in the ALTERNATE screen (claude ≥ 2.1.178, opencode) has no scrollback there
    // to wipe, and its repaint redraws the current frame in place — the same nudge REPAINT and the
    // tab-refocus path (#503) send. So once the attach settles, an alt-screen owner always
    // repaints; a normal-buffer session keeps the heuristics.
    let attachBytes = 0;
    let initialTailLock = false;
    // This socket's claim verdict (the role frame arrives right after open, long before the
    // settle). A read-only secondary never drives the pty, so the unconditional repaint is the
    // owner's alone — as on refocus (#503).
    let sockRole: TermRole = "owner";
    const altScreenOwner = () =>
      sockRole === "owner" && term.buffer.active.type === "alternate";
    /** How long an attach gets to deliver its replay before the repaint. */
    const ATTACH_SETTLE_MS = 800;
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
          if (altScreenOwner()) {
            jiggleRows();
            return;
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
        }, ATTACH_SETTLE_MS),
      );
    };
    // A reconnect resumes from its offset: the tail lock and the ↓ FAB reveal above belong to a
    // fresh attach, so a reader scrolled into history stays where they are. Only the redraw, and
    // only in the alt screen (see above). It ADDS its timer and never clears the others: a
    // reconnect inside the first ATTACH_SETTLE_MS would otherwise cancel the fresh attach's
    // settle, and with it the tail-lock release — every later chunk would then drag a reader
    // who scrolled up by the scrollbar back to the tail (Hermes on #1204).
    const repaintAfterReconnect = () => {
      jiggleTimers.push(
        setTimeout(() => {
          if (sock !== sockRef.current || !altScreenOwner()) return;
          jiggleRows();
        }, ATTACH_SETTLE_MS),
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
    // reported "selecting text scrolls the terminal around." Released on selection end
    // (mouseup / exit select mode); the live-output `follow` is gated on `!selectionActive`.
    //
    // #812: the pin holds the BUFFER position, not just the DOM `scrollTop`, and that is the
    // whole fix. xterm's drag-select edge auto-scroll does not touch `scrollTop` — it fires
    // `onRequestScrollLines`, which moves `ydisp` (the buffer's viewport line) and lets the DOM
    // follow. So a pin that only rewrote `scrollTop` was arguing with the wrong layer: the
    // buffer kept walking toward the top on its ~50 ms tick while we shoved the element back,
    // and the rendered viewport juddered once per tick before the buffer won outright.
    //
    // Traced, 25 ms sampling of `.xterm-viewport.scrollTop` through one 500 ms drag:
    //
    //     pass  1305→945→1305→765→1305→405→1305→225→1305→0→1305
    //     FAIL  1305→585→1305→45→1305→0
    //
    // Every run bounced; the failures were just the ones whose last tick landed displaced. So
    // restore `ydisp` — the source of truth — and the DOM never has a wrong value to show.
    // `term.onScroll` fires synchronously on a `scrollLines` scroll, before the queued render,
    // so the corrected position is what gets painted rather than a corrected-next-frame one.
    let selectionActive = false;
    let selectionPinTop = 0;
    let selectionPinY = 0;
    // `scrollToLine` re-enters `onScrolled` through `term.onScroll`; without this the restore
    // would recurse once per correction.
    let restoringPin = false;
    type ViewportAnchor = { viewportY: number; scrollTop: number };
    let readerAnchor: ViewportAnchor | null = null;
    const beginSelectionPin = () => {
      selectionActive = true;
      selectionPinTop = vpEl?.scrollTop ?? 0;
      selectionPinY = term.buffer.active.viewportY;
    };
    const endSelectionPin = () => {
      selectionActive = false;
    };
    /** Put both layers back where the selection started. Returns whether anything moved. */
    const restoreSelectionPin = (): boolean => {
      if (restoringPin) return false;
      let moved = false;
      restoringPin = true;
      try {
        // Buffer first: the DOM value is derived from it, so correcting `scrollTop` against a
        // moved `ydisp` is undone by the next render.
        if (term.buffer.active.viewportY !== selectionPinY) {
          term.scrollToLine(selectionPinY);
          moved = true;
        }
        if (vpEl && vpEl.scrollTop !== selectionPinTop) {
          vpEl.scrollTop = selectionPinTop;
          moved = true;
        }
      } finally {
        restoringPin = false;
      }
      return moved;
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
      tailRequested = false; // a real gesture takes the viewport back from the ↓ jump (#1108)
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
      // #559/#812: hold the viewport still while a selection is in progress — drag-select edge
      // auto-scroll (or a stray follow) just tried to move it; put BOTH the buffer line and the
      // element back to where the selection began, so the highlighted text stays under the
      // finger/cursor. Restoring only the element left the buffer moved and the view juddering.
      if (selectionActive) {
        if (restoreSelectionPin()) return;
      }
      settleTailRequest();
      if (tailRequested) readerAnchor = null;
      else if (
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
          const displayBytes = stripLiveScrollbackErase.strip(
            engine,
            !attachReplayOpen,
            b,
          );
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
            // The heuristic backstop runs on a TRUE fresh attach (consumed offset 0). A caught-up
            // reconnect (have == total) receives no delta while the screen is already painted —
            // jiggling a normal-buffer frame would wipe it (Hermes #374); an alt-screen one is
            // redrawn in place, so that one still repaints.
            if (sock.consumed === 0) armRepaintBackstop();
            else repaintAfterReconnect();
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
          stripLiveScrollbackErase.reset();
        },
        onRole: (r, h) => {
          sockRole = r;
          setRole(r);
          onRoleRef.current?.(r);
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

    refitSoonRef.current = refitSoon;

    // A keystroke typed into the terminal inside a compose delivery's paste→Enter window is
    // held by Compose and written right after that Enter (Hermes on #908, round 5); otherwise
    // it goes to the pty as it always has.
    term.onData((d) => {
      if (!composeRef.current?.deferInput(d)) sock.send({ t: "i", d });
    });
    term.onResize(sendResize);
    // A separate listener, not folded into sendResize: that one dedupes on (cols, rows) and
    // returns early, which is right for the pty and wrong for a readout that must also be
    // correct on the very first fit.
    term.onResize(({ cols: c }) => setCols(c));
    setCols(term.cols);

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
          openTerminalLink(url);
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
    requestTailRef.current = () => {
      readerAnchor = null;
      tailRequested = true;
    };

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
      refitSoonRef.current = () => {}; // …and neither must a late size change (#859)
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
      requestTailRef.current = () => {};
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

  // Re-theme, re-size AND re-FACE the live terminal WITHOUT tearing it down. Colours apply
  // immediately; a size or family change recomputes the grid, and xterm's onResize handler
  // (wired above) pushes the new dimensions to the pty — SIGWINCH, so the agent re-lays-out at
  // the new width. The cursor tracks the brand accent (#211 Phase 2).
  //
  // Size (#859) and family (#866) are two axes on one effect deliberately: they are the two
  // inputs to the same cell measurement, so coalescing them here means changing both in one
  // commit costs ONE refit rather than two. Neither may join the socket effect's deps.
  //
  // The refit goes through `refitSoonRef` — the DEBOUNCED path the socket effect owns — never a
  // raw fit(). A theme flip is rare, but the #859 stepper is tapped repeatedly, and one SIGWINCH
  // per tap is exactly the resize storm #227/#349 exist to coalesce: a repaint-heavy TUI piles
  // those frames into scrollback as duplicated/garbled content. One debounce implementation for
  // every caller, and the socket effect's dep array stays identity-only.
  useEffect(() => {
    const term = termRef.current;
    if (!term) return;
    term.options.theme = { ...xtermTheme(theme), cursor: accent };
    term.options.fontFamily = termFontFamily;
    term.options.fontSize = termFontSize;
    refitSoonRef.current();
  }, [theme, accent, termFontSize, termFontFamily]);

  const text = statusText(status);
  // #284: use the server-resolved display title only (manual rename → AI title → meaningful
  // first message, else ""). Never fall back to the RAW first message, or a stray "a" / "."
  // leaks into the panel header — drop straight to the short id.
  const title = row?.title || `${id.slice(0, 8)}…`;
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
  // An engine with no agent behind it (the plain shell, `kind = "terminal"` in its manifest —
  // #853 P4) has nothing to hand off; read from the roster, never an id.
  const canHandoff = isAgent(engine) && !actionNative.startsWith("new-");
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
    // #1108: `scrollToBottom()` moves xterm's buffer at once, but `.xterm-viewport` catches up
    // only on xterm's next render. In between, buffer and DOM disagree, and both onScrolled and
    // the output path read "buffer at tail, DOM not" as a reader sitting off the tail: they
    // recorded the old position as an anchor and restored it on the next chunk (back to the top),
    // or declined to follow (left one frame short). Mark the tail as REQUESTED first, so that
    // gap reads as what it is. Don't write `.xterm-viewport.scrollTop` here either: xterm owns it,
    // and a direct write (the first cut of this fix) still left the view one frame short on a
    // loaded runner.
    requestTailRef.current();
    termRef.current?.scrollToBottom();
    setAtBottom(true);
    // Setters in the dep array are semantically inert (React guarantees they are stable) —
    // they are declared for the React compiler's inference (#1109): with the chrome-slot
    // portal added to this component, the compiler's inferred deps for this callback include
    // the setters it writes, and an empty manual array reads as an unpreservable
    // memoization, which skips optimizing the whole component.
  }, [setAppScrolledUp, setAppTailUnknown, setAtBottom]);
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
    // See scrollToTail above: declared for the compiler's inferred deps, semantically inert.
  }, [setTakeoverEpoch]);
  // Order: Files leads (the new primary affordance); Repaint stays ahead of the fold because
  // burying the recovery control when the screen is blank would be the wrong trade.
  // An array LITERAL with conditional entries, not an imperative `push` — mutating an array during
  // render made the compiler treat the captured callbacks (which read refs) as render-time ref
  // access. The compiler memoizes this for us, so no manual useMemo either.
  const headActions: HeadAction[] = [
    // Files leads: it is the new primary affordance.
    ...(onToggleFiles
      ? [
          {
            id: "files",
            label: "Files",
            aria: "Browse session files",
            title:
              filesDisabledReason ?? "Browse this session's files and folders",
            icon: <PanelRight size={13} aria-hidden="true" />,
            active: filesOpen,
            disabled: Boolean(filesDisabledReason),
            run: (trigger?: HTMLElement | null) => onToggleFiles(trigger),
          },
        ]
      : []),
    // Repaint stays ahead of the fold: burying the recovery control when the screen is blank
    // would be the wrong trade.
    ...(role === "owner"
      ? [
          {
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
          },
        ]
      : []),
    {
      id: "recap",
      label: "Recap",
      aria: "Open session brief",
      title:
        "Session brief: full title, summary, and a chronological recap of this session",
      icon: <ScrollText size={13} aria-hidden="true" />,
      run: (trigger?: HTMLElement | null) => {
        setRecapTrigger(
          trigger ?? (document.activeElement as HTMLElement | null),
        );
        setRecapOpen(true);
      },
    },
    ...(canHandoff
      ? [
          {
            id: "handoff",
            label: "Hand off",
            aria: "Hand off session to another engine",
            title:
              "Hand off: start a new session in another engine, seeded with this session's context",
            icon: <ArrowLeftRight size={13} aria-hidden="true" />,
            run: (trigger?: HTMLElement | null) => {
              setHandoffTrigger(
                trigger ?? (document.activeElement as HTMLElement | null),
              );
              setHandoffOpen(true);
            },
          },
        ]
      : []),
    // Adopt to mission / Open mission (#948 P5). POSITION: after Hand off, before To map — the head
    // folds from the END (#783), so Hand off stays on the bar. Offered only when the row KNOWS the
    // membership (`mission` absent = the store could not be read), never on an archived session and
    // never on an unreconciled `new-<uuid>` placeholder, which the server's canonical_key refuses.
    // It acts on `actionKey` — the id the URL settled on — never the frozen transport identity.
    ...(row && row.mission !== undefined && !row.archived && !isNewSessionPlaceholder(actionKey)
      ? [
          row.mission
            ? {
                id: "mission",
                label:
                  row.mission.title.length > 28
                    ? `${row.mission.title.slice(0, 27)}…`
                    : row.mission.title,
                aria: `Open mission ${row.mission.title}`,
                title: `Open mission: ${row.mission.title} (${row.mission.state})`,
                icon: <Crosshair size={13} aria-hidden="true" />,
                active: true,
                run: () => navigate(missionLink(row.mission!.id)),
              }
            : {
                // The SAME id as Open mission: HeadActions keys its buttons by id, so a successful
                // adoption relabels this button instead of unmounting the dialog's opener (#953).
                id: "mission",
                label: "Adopt to mission",
                aria: "Adopt this session into a mission",
                title: "Adopt to mission: add this session to an open mission",
                icon: <Crosshair size={13} aria-hidden="true" />,
                run: (trigger?: HTMLElement | null) => {
                  const opener = trigger ?? (document.activeElement as HTMLElement | null);
                  adoptHeadRef.current = opener?.closest<HTMLElement>("[data-fit-width]") ?? null;
                  setAdoptTrigger(opener);
                  setAdoptOpen(true);
                },
              },
        ]
      : []),
    // "To map" (#936) — the inverse of a window's ⤢, and the only way back into window mode
    // once a session has been opened full screen. Passed only by `SessionView`, so it never
    // appears inside a window.
    //
    // POSITION IS LOAD-BEARING: after Hand off, before the zoom pair. HeadActions folds from the
    // END (#783), so anything inserted earlier pushes Hand off into the "…" overflow and turns
    // session-recap.spec.ts red — which is the contract working, not a stale test.
    //
    // Position alone was not enough. #744/#859's coarse-pointer contract is that at 420px all SIX
    // icon-only chips fit and nothing folds; a seventh broke it, so `SessionView` withholds this
    // one below the ≤800px breakpoint — which it owes the operator anyway, since a phone can
    // never host a window. (Since #948 P6 a phone shows one Actions menu, so that fold no longer
    // happens below 800px; the withholding stays for the window reason.)
    ...(onToMap
      ? [
          {
            id: "to-map",
            label: "To map",
            aria: "Open this session as a window on the map",
            title:
              "To map: open this session as a floating window on the overview map, alongside the others",
            icon: <SquareDashedBottom size={13} aria-hidden="true" />,
            run: () => onToMap(),
          },
        ]
      : []),
    // Quick zoom (#859). On a phone the font size IS the agent's column count, so this is the
    // difference between a TUI that lays its panels out and one that collapses to a 3-character
    // label column. It lives here rather than only in Settings because the moment you need it is
    // the moment you are looking at the unreadable pane.
    //
    // LAST IN THE ARRAY, deliberately — do not promote it. HeadActions folds from the END (#783),
    // and #744's coarse-pointer contract is that every action stays ONE TAP away on touch (above
    // the ≤800px breakpoint; below it every action is in one Actions menu, #948 P6). Six
    // icon-only chips do not fit a 300px pane, so something must fold there; putting the newest,
    // least critical pair last means it is these two and never Hand off. Inserting them earlier
    // pushed Hand off into the overflow and turned session-recap.spec.ts red — which is the
    // contract working, not a stale test.
    {
      id: "text-smaller",
      label: "Smaller text",
      aria: "Smaller terminal text",
      title: `Smaller terminal text — a wider terminal for the agent${cols ? ` (now ${cols} columns)` : ""}`,
      icon: <AArrowDown size={13} aria-hidden="true" />,
      disabled: termFontSize <= TERM_FONT_SIZE_MIN,
      run: () => setTermFontSize(stepTermFontSize(termFontSize, -1)),
    },
    {
      id: "text-bigger",
      label: "Bigger text",
      aria: "Bigger terminal text",
      title: `Bigger terminal text — a narrower terminal for the agent${cols ? ` (now ${cols} columns)` : ""}`,
      icon: <AArrowUp size={13} aria-hidden="true" />,
      disabled: termFontSize >= TERM_FONT_SIZE_MAX,
      run: () => setTermFontSize(stepTermFontSize(termFontSize, 1)),
    },
    // Share link (#1232) — the installed app has no address bar, so this is how a session is passed
    // on from it. AFTER the zoom pair, the new last entry: HeadActions folds from the END (#783), so
    // nothing already on the bar moves. Withheld on an unreconciled `new-<uuid>` placeholder, whose
    // URL names a session the server does not have yet; it acts on `actionKey`, the settled id.
    ...(!isNewSessionPlaceholder(actionKey)
      ? [
          {
            id: "share-link",
            label: "Share link",
            aria: "Share a link to this session",
            title: "Share link: send or copy a link that opens this session",
            icon: <Share2 size={13} aria-hidden="true" />,
            run: () => {
              void shareLink({
                title: row?.title || "BattleLab session",
                path: `/s/${actionKey.slice(0, actionKey.indexOf(":"))}/${actionNative}`,
              }).then((outcome) => {
                if (outcome !== "copied" && outcome !== "failed") return;
                const tick = Date.now();
                setLinkToast({ tick, ok: outcome === "copied" });
                window.setTimeout(
                  () => setLinkToast((t) => (t.tick === tick ? { tick: 0, ok: true } : t)),
                  outcome === "copied" ? COPIED_TOAST_MS : COPY_FAILED_TOAST_MS,
                );
              });
            },
          },
        ]
      : []),
  ];

  return (
    <div className={styles.wrap}>
      {/* Panel header (#211 4c, re-cut in #744): a HUD meta run — semantic LED, engine box,
          project, relative update time — mirroring what the sidebar row shows for this session,
          then the action buttons. The session title is deliberately absent: it is the sidebar's
          job and the session brief's, and the 26px bar reads better carrying facts the sidebar
          can't repeat next to the live pane (which project, how stale).
          #1109: the facts run is the shared `HeadFacts` component — a map window's chrome renders
          the SAME run, and `suppressHead` removes this bar entirely there so a window never
          shows two stacked bars. The actions either stay here (the pane's own fold) or portal
          into the window chrome's slot, whose single ⋯ menu carries the overflow. */}
      {!suppressHead && (
        <div className={styles.panelHead} data-panel-head="">
          <span className={styles.headLeft}>
            <HeadFacts engine={engine} status={status} row={row} />
          </span>
          {/* Actions with measured overflow (#783). A fourth labelled button breaks the header's
              own measured contract (see Terminal.module.css), so trailing actions fold into a "…"
              menu that still carries full labels — the KeyBar idiom, not an icon-only shrink. */}
          <HeadActions
            className={styles.headActions}
            btnClassName={styles.restartBtn}
            labelClassName={styles.headActionLabel}
            actions={headActions}
            collapsed={isMobile}
          />
        </div>
      )}
      {suppressHead &&
        headActionsSlot &&
        createPortal(
          <HeadActions
            className={styles.headActions}
            btnClassName={styles.restartBtn}
            labelClassName={styles.headActionLabel}
            actions={headActions}
            collapsed={isMobile}
            reservePx={headReservePx}
            foldInto="external"
            overflowRef={headOverflowRef}
            barRef={headBarRef}
          />,
          headActionsSlot,
        )}
      {adoptOpen && row && (
        <AdoptToMissionModal
          session={row}
          sessionKey={actionKey}
          onClose={() => setAdoptOpen(false)}
          // A session the sidebar's page does not hold lives in the lookup cache instead; the list
          // listener cannot reach that copy, so it is updated here.
          onAdopted={(mission) => remember(actionKey, { ...row, mission })}
          returnFocusTo={adoptTrigger}
          resolveReturnFocus={resolveAdoptReturn}
        />
      )}
      {recapOpen && (
        <SessionRecapModal
          sessionId={actionKey}
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
          sessionId={actionKey}
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
        {linkToast.tick !== 0 && (
          <div
            key={linkToast.tick}
            className={
              linkToast.ok
                ? styles.copiedToast
                : `${styles.copiedToast} ${styles.copyFailed}`
            }
            role="status"
            aria-live="polite"
            data-link-toast=""
          >
            {linkToast.ok ? "Link copied" : "Copy needs a secure origin"}
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
      {/* #477: persist the compose draft server-side per session, under the DURABLE key —
          the converged `rowKey`, never this pane's frozen placeholder identity (#908 round 5).
          A not-yet-real `new-…` placeholder has no metadata key → drafts disabled until then. */}
      <Compose
        ref={composeRef}
        sendInput={sendInput}
        connEpoch={connEpoch}
        waitInputReady={waitInputReady}
        defaultOpen={composeDefaultOpen}
        sessionId={draftSessionKey(engine, id, rowKey)}
        onSaveAsTemplate={onSaveAsTemplate}
        onOpenGallery={onOpenGallery}
      />
    </div>
  );
}
