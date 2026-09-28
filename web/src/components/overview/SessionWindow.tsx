import { Maximize2, MoreHorizontal, X } from "lucide-react";
import {
  memo,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { useNavigate } from "react-router-dom";
import { useSessionRow } from "../../app/useSessionRow";
import { projectColor } from "../../lib/format";
import { pathToken } from "../../lib/pathToken";
import type { TermRole, TermStatus } from "../../lib/termSocket";
import type { FreshSession } from "../../lib/termUrl";
import { FilePanel } from "../files/FilePanel";
import { panelMode } from "../files/filePanelLayout";
import type { HeadAction } from "../terminal/HeadActions";
import { HeadFacts } from "../terminal/HeadFacts";
import type { TemplateDraft } from "../terminal/Compose";
import { Terminal, type TerminalHandle } from "../terminal/Terminal";
import {
  MenuPopover,
  type MenuAnchor,
  type RowMenuEntry,
} from "../sidebar/RowMenu";
import styles from "./sessionWindow.module.css";
import { clampRect, type Rect, type Size } from "./workspace";
import { useEngineRoster } from "../../app/engineRoster";
import { RuntimeGate } from "../terminal/RuntimeGate";

/** One floating session window on the Overview map (#208).
 *
 *  A window is the EXISTING session pane with ONE bar on top — `<Terminal>` is mounted with
 *  the same props the `/s/:engine/:id` route gives it (its own `panelHead` suppressed via
 *  `suppressHead`, #1109), so there is no second terminal implementation to keep in step. The
 *  chrome bar carries the facts the pane's bar used to render — through the SAME `HeadFacts`
 *  component, fed by this window's own row lookup — plus the session title, the action chips
 *  (which portal in from the pane), and the window controls. Everything else this component
 *  adds is chrome: drag, resize, focus/raise, and the controls.
 *
 *  Three deliberate non-features:
 *  - **It never calls `fit()`.** Changing the window's CSS size is enough; the pane's own
 *    ResizeObserver → debounced `refitSoon` (#859) does the refit, which is what stops a drag
 *    from marching the agent through every intermediate width (the #227/#349 resize storm).
 *    The file drawer changes nothing about that: it OVERLAYS the pane, so the agent's grid
 *    never moves when it opens or closes.
 *  - **It never touches the socket.** Mount/unmount is the whole lifecycle: closing the window
 *    unmounts `<Terminal>`, which tears its own socket and document-level listeners down.
 *  - **Window-level exits do not guard an unsaved editor draft.** ✕, Close all and an
 *    archive-from-elsewhere unmount the pane (and with it the viewer) the way a browser tab
 *    close would: the viewer's own `beforeunload` guard covers a reload, an in-flight save is
 *    lease-bound server-side (applied, or its displaced copy retained — never lost, #950), and
 *    a dirty UNSAVED draft is lost. The full-screen pane's dirty-guard paths (Esc, ✕, router
 *    nav) are unchanged. Recorded in the PR for #1109. */

/** The pane actions the session group already carries, so a folded chip never names its modal
 *  twice in one menu (#1109): Recap → the session menu's "Session brief", Hand off → its
 *  "Hand off…", mission adopt/open → its mission group. Applied ONLY on the merged (on-map)
 *  menu; off the map the session group is absent, so the pane menu carries everything. */
const SESSION_COVERED_PANE_IDS = new Set(["recap", "handoff", "mission"]);

/** What the chrome bar must hold besides the action chips: the grip, the three window buttons
 *  (⋯ ⤢ ✕), a minimum of title, and the bar's own gaps. The measured facts run is added on
 *  top (`reservePx`), so the fold is decided against what the bar really has — folding a
 *  little early is the safe direction; folding late would clip the title. */
const CHROME_RESERVE = 190;

export const SessionWindow = memo(function SessionWindow({
  wkey,
  engine,
  id,
  actionKey,
  title,
  fresh,
  rect,
  bounds,
  focused,
  role,
  onFocus,
  onClose,
  onFullScreen,
  onRect,
  onRole,
  onReconcile,
  onMenu,
  offMapReason,
}: {
  /** The engine-qualified session id. Passed back to every handler so the handlers themselves
   *  can be the workspace's own stable callbacks rather than per-render closures — that is what
   *  lets `memo` above actually hold, and a mounted terminal is an expensive thing to re-render
   *  because the map moved. */
  wkey: string;
  engine: string;
  id: string;
  /** The id the SERVER should act on — the engine's real id after a converge, the transport key
   *  before one. Handed straight to `<Terminal rowKey>`, which is the pane's own name for the
   *  same split (#867): without it a converged window's Recap "Review now" and its Hand off
   *  gate keep naming the `new-<uuid>` placeholder, i.e. a session that does not exist. */
  actionKey: string;
  title: string;
  /** Fresh-launch params for a window opened straight from the new-session flow (#936). The
   *  pane freezes them itself (`freshRef`), so this is read once at mount and never again —
   *  except for `fresh.cwd`, which is the file drawer's directory until a row reports one. */
  fresh?: FreshSession;
  rect: Rect;
  /** The overlay box. Every move/resize is clamped against it, so a window can never be put
   *  somewhere it cannot be dragged back from. */
  bounds: Size;
  focused: boolean;
  /** Mirrors the pane's own #184/#293 verdict; the pane renders the banner + Take over itself. */
  role: TermRole;
  onFocus: (key: string) => void;
  onClose: (key: string) => void;
  onFullScreen: (key: string) => void;
  onRect: (key: string, rect: Rect) => void;
  onRole: (key: string, role: TermRole) => void;
  /** The engine reconciled a `new-` placeholder to its real id (#127/#315). The window's
   *  transport identity does NOT change — see `WorkspaceWindow.actionKey`. */
  onReconcile: (key: string, sid: string) => void;
  /** Open the session menu (#968) for this window's session: from the ⋯ in the chrome, or a
   *  right-click on the title bar. Stable, like every handler above. #1109 extends it with the
   *  window's folded pane actions, which the canvas merges into the ONE menu as a labelled
   *  second group. */
  onMenu?: (
    key: string,
    anchor: MenuAnchor,
    opener: HTMLElement | null,
    paneItems?: RowMenuEntry[],
  ) => void;
  /** Set while the session is not on the map (filtered off), which is where the menu reads its
   *  row from. #1109: this no longer disables the ⋯ — it only withdraws the ROW-DEPENDENT
   *  session actions. The ⋯ stays enabled and opens the pane actions alone, so a narrow
   *  filtered window keeps Repaint / Files / text size reachable (Hermes on #1109). The reason
   *  rides the control's title, so the state still explains itself. */
  offMapReason?: string;
}) {
  // Re-render when the engine roster lands or changes (#853 P4): this renders agent names,
  // badges or colours, which come from the roster, not from a client-side list.
  useEngineRoster();
  const [dragging, setDragging] = useState(false);
  // The template picker and "Save as template" leave through the ROUTER, never a document
  // navigation: a full unload could abort the draft flush the composer issues on the way out —
  // the loss SessionView's callbacks were added to prevent, and this host shares the same
  // `<Terminal>` (Hermes on #908, round 9). `navigate` is stable, so `memo` still holds.
  const navigate = useNavigate();
  const onOpenGallery = useCallback((to: string) => navigate(to), [navigate]);
  const onSaveAsTemplate = useCallback(
    (draft: TemplateDraft) => navigate("/templates/new", { state: { prefill: draft } }),
    [navigate],
  );
  // Pointer origin + the rect at gesture start. A ref, not state: it is read inside the move
  // handler and must never re-render on its own.
  const gestureRef = useRef<{ px: number; py: number; rect: Rect } | null>(null);
  const menuBtnRef = useRef<HTMLButtonElement>(null);
  const headRef = useRef<HTMLDivElement>(null);

  // ---- the chrome's facts run (#1109) --------------------------------------------------------
  // The same row accessor the pane reads (#867): the sidebar's list first, a per-session lookup
  // as the fallback. A placeholder (`new-<uuid>`) is never requested — the store refuses the
  // shape — and until the converge lands the chrome degrades to LED + engine + title, exactly
  // as a bare pane header does.
  const row = useSessionRow(`${engine}:${id}`, actionKey);
  // The LED is THIS browser's attachment state — a fact only the pane's socket knows, so the
  // pane publishes it (`onTermStatus`, through the status write itself) and the chrome renders
  // it through the same HeadFacts run. The initial states match on purpose: the pane's opening
  // "connecting" needs no publish.
  const [status, setStatus] = useState<TermStatus>({ kind: "connecting" });
  const onTermStatus = useCallback((s: TermStatus) => setStatus(s), []);
  // The tint, computed exactly the way the pane's project chip does (#1109). Only a real
  // project entity tints; a folder-cwd session keeps the plain chrome ground.
  const proj =
    row?.project.kind === "project"
      ? row.project.color || projectColor(row.project.id)
      : undefined;

  // The facts run's measured width — the fold's reserve is only honest if it knows what the
  // bar really carries. `flex: none`, so offsetWidth IS its natural width; the container-query
  // ladder (which hides the time, then the tag) shows up here as real resizes.
  const factsRef = useRef<HTMLSpanElement>(null);
  const [factsW, setFactsW] = useState(0);
  useLayoutEffect(() => {
    const el = factsRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => setFactsW(el.offsetWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // ---- the file drawer (#1109) ---------------------------------------------------------------
  // The panel needs a real starting directory: the row's cwd, else the fresh launch's (a
  // window opened from the new-session flow carries one in its record before any row exists —
  // the same order SessionView resolves). Remember the last cwd KEYED ON THE WINDOW KEY, which
  // is frozen for life: `fresh` is dropped by nothing here but the row can lag a poll, and the
  // drawer must not flicker out in the gap. The key matters exactly as in SessionView — an
  // unkeyed cache would survive into a DIFFERENT session's window.
  const resolvedCwd = row?.cwd || fresh?.cwd || "";
  const [seenCwd, setSeenCwd] = useState<{ key: string; cwd: string }>({
    key: wkey,
    cwd: resolvedCwd,
  });
  if (seenCwd.key !== wkey) setSeenCwd({ key: wkey, cwd: resolvedCwd });
  else if (resolvedCwd && resolvedCwd !== seenCwd.cwd)
    setSeenCwd({ key: wkey, cwd: resolvedCwd });
  const cwd = resolvedCwd || (seenCwd.key === wkey ? seenCwd.cwd : "");

  const [filesOpen, setFilesOpen] = useState(false);
  const [filesTrigger, setFilesTrigger] = useState<HTMLElement | null>(null);
  const termRef = useRef<TerminalHandle>(null);
  // The window BODY is the "pane" for filePanelLayout's dock/sheet math (#1109): the drawer
  // docks when the body can spare TERM_MIN + MIN_W, and rides as a sheet over it otherwise.
  const bodyRef = useRef<HTMLDivElement>(null);
  const [bodyW, setBodyW] = useState(0);
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => setBodyW(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const filesMode = panelMode(bodyW);

  // ---- the ONE menu (#1109) ------------------------------------------------------------------
  // The pane's chips portal into the chrome bar and fold (measured, like the pane's own fold)
  // into THE chrome ⋯ — published through this ref, read at menu-open time.
  const paneOverflowRef = useRef<HeadAction[]>([]);
  // The chrome's chips slot, as state: <Terminal> receives the ELEMENT to portal into, which
  // only exists after this bar has committed. One extra frame on mount, before any socket has
  // anything to show.
  const [actionsSlot, setActionsSlot] = useState<HTMLSpanElement | null>(null);
  // The off-map pane-only menu, hosted HERE: the canvas menu reads the row from the map, and
  // off the map there is no row — but the pane actions never needed one.
  const [paneMenuAnchor, setPaneMenuAnchor] = useState<MenuAnchor | null>(null);

  /** The folded pane actions as menu entries. `dedupe` drops the actions the session group
   *  already carries (only meaningful when the session group IS rendered); the opener is the
   *  ⋯ the menu returns focus to — a menu item unmounts with its menu. */
  const paneMenuItems = useCallback(
    (opener: HTMLElement | null, dedupe: boolean): RowMenuEntry[] =>
      paneOverflowRef.current
        .filter((a) => !(dedupe && SESSION_COVERED_PANE_IDS.has(a.id)))
        .map((a) => ({
          key: a.id,
          label: a.label,
          ariaLabel: a.aria,
          icon: a.icon,
          disabled: a.disabled,
          onSelect: () => a.run(opener),
        })),
    [],
  );

  const openMergedMenu = useCallback(
    (anchor: MenuAnchor) => {
      const opener = menuBtnRef.current;
      if (offMapReason) {
        setPaneMenuAnchor(anchor);
        return;
      }
      onMenu?.(wkey, anchor, opener, paneMenuItems(opener, true));
    },
    [offMapReason, onMenu, wkey, paneMenuItems],
  );

  const startGesture = (
    e: ReactPointerEvent<HTMLElement>,
    kind: "move" | "resize",
  ) => {
    // The header is the only drag initiator: a press that lands on ⋯, ⤢, ✕, the grip or one of
    // the portalled action chips must not start a window drag, or every click on a control
    // would nudge the window first.
    if (kind === "move" && (e.target as HTMLElement).closest("button")) return;
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    onFocus(wkey);
    gestureRef.current = { px: e.clientX, py: e.clientY, rect };
    setDragging(true);
    const el = e.currentTarget;
    el.setPointerCapture(e.pointerId);

    const onMove = (ev: globalThis.PointerEvent) => {
      const g = gestureRef.current;
      if (!g) return;
      const dx = ev.clientX - g.px;
      const dy = ev.clientY - g.py;
      onRect(
        wkey,
        clampRect(
          kind === "move"
            ? { ...g.rect, x: g.rect.x + dx, y: g.rect.y + dy }
            : { ...g.rect, w: g.rect.w + dx, h: g.rect.h + dy },
          bounds,
        ),
      );
    };
    const onUp = () => {
      gestureRef.current = null;
      setDragging(false);
      el.releasePointerCapture?.(e.pointerId);
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerup", onUp);
      el.removeEventListener("pointercancel", onUp);
    };
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerup", onUp);
    el.addEventListener("pointercancel", onUp);
  };

  /** Keyboard resize, so the grip is not a pointer-only control. */
  const onResizeKey = (e: React.KeyboardEvent) => {
    const step = e.shiftKey ? 40 : 10;
    const d: Record<string, [number, number]> = {
      ArrowRight: [step, 0],
      ArrowLeft: [-step, 0],
      ArrowDown: [0, step],
      ArrowUp: [0, -step],
    };
    const move = d[e.key];
    if (!move) return;
    e.preventDefault();
    onRect(
      wkey,
      clampRect({ ...rect, w: rect.w + move[0], h: rect.h + move[1] }, bounds),
    );
  };

  return (
    <section
      className={`${styles.win}${focused ? ` ${styles.focused}` : ""}${dragging ? ` ${styles.dragging}` : ""}`}
      style={{
        left: rect.x,
        top: rect.y,
        width: rect.w,
        height: rect.h,
        ...(proj ? ({ "--proj": proj } as CSSProperties) : {}),
      }}
      // Capture, so raising happens before the terminal swallows the press for selection.
      onPointerDownCapture={() => onFocus(wkey)}
      aria-label={`Session window: ${title}`}
      data-session-window={`${engine}:${id}`}
      data-focused={focused ? "true" : "false"}
      data-tinted={proj ? "true" : undefined}
    >
      <div
        ref={headRef}
        className={styles.head}
        onPointerDown={(e) => startGesture(e, "move")}
        // Right-click on the title bar opens THE menu at the pointer (#968/#1109): the merged
        // session+pane menu when the row is on the map, the pane-only menu when it is not. A
        // hostless window (the ⋯ itself absent) leaves the browser's own menu alone.
        onContextMenu={(e) => {
          if (!onMenu) return;
          e.preventDefault();
          openMergedMenu({ point: { x: e.clientX, y: e.clientY } });
        }}
        data-window-head
      >
        <span className={styles.grip} aria-hidden="true">
          ⣿
        </span>
        {/* The facts run (#1109): the SAME HeadFacts the pane's own bar renders — status LED,
            engine badge, the session's custom tag, mission tag, project · relative time — fed
            by this window's row lookup and the pane's published socket status. `flex: none`:
            the run is a FACT the fold budgets for, and it yields through the container ladder
            instead of by flex pressure. */}
        <span ref={factsRef} className={styles.facts} data-window-facts="">
          <HeadFacts engine={engine} status={status} row={row} showTag />
        </span>
        {/* The pane's own header deliberately omits the title (it is the sidebar's job there);
            a floating window has no sidebar beside it, so the chrome carries it — and it is
            the bar's shrink absorber, ellipsising before anything clips. */}
        <span className={styles.title} title={title}>
          {title}
        </span>
        {/* The pane's action chips portal here from <Terminal> (#1109): the fold measures this
            bar and overflows into the ONE ⋯ menu below, never into a second chip of its own. */}
        <span
          ref={setActionsSlot}
          className={styles.actionsSlot}
          data-window-actions-slot=""
        />
        {role === "secondary" && (
          <span className={styles.lock} data-window-readonly>
            READ-ONLY
          </span>
        )}
        <span className={styles.btns}>
          {onMenu && (
            <button
              ref={menuBtnRef}
              type="button"
              aria-label="Session actions"
              title={offMapReason ? `Session actions unavailable — ${offMapReason}` : "Session actions"}
              aria-haspopup="menu"
              onClick={(e) => openMergedMenu({ element: e.currentTarget })}
              data-window-menu
            >
              <MoreHorizontal size={13} aria-hidden="true" />
            </button>
          )}
          <button
            type="button"
            aria-label="Open full screen"
            title="Open full screen"
            onClick={() => onFullScreen(wkey)}
            data-window-fullscreen
          >
            <Maximize2 size={13} aria-hidden="true" />
          </button>
          <button
            type="button"
            aria-label="Close window"
            title="Close window"
            onClick={() => onClose(wkey)}
            data-window-close
          >
            <X size={13} aria-hidden="true" />
          </button>
        </span>
      </div>

      {/* The off-map pane-only menu (#1109). The session group needs the map's row, which is
          exactly what this session has lost — so only the pane group renders, and the control's
          title carries the reason (see the ⋯ above). */}
      {paneMenuAnchor && (
        <MenuPopover
          items={paneMenuItems(menuBtnRef.current, false)}
          title={title}
          label="Pane actions"
          anchor={paneMenuAnchor}
          ownerRef={menuBtnRef}
          // The menu asks for refocus when it closes by Escape or after a keyboard selection
          // (a menu item unmounts with its menu); honoring it keeps focus off BODY (#1109
          // review) — the ⋯ that opened the menu is where focus returns to.
          onClose={(refocus?: boolean) => {
            setPaneMenuAnchor(null);
            if (refocus) menuBtnRef.current?.focus();
          }}
        />
      )}
      <div ref={bodyRef} className={styles.body}>
        {/* RuntimeGate (#1132): a REMOVED engine renders its attach-only retirement notice
            instead of the pane — and with the pane gone, its portaled chips (and therefore
            the Files toggle) never render either, so the drawer below stays unreachable for
            a retired engine, exactly as before. */}
        <RuntimeGate engine={engine} id={id}>
        <Terminal
          engine={engine}
          id={id}
          // Transport (`engine`/`id`) frozen, ACTIONS follow the converge — the same two
          // identities the record keeps, handed to the pane under the name it already uses.
          rowKey={actionKey}
          fresh={fresh}
          // The converge lands on the WORKSPACE, never on this component's own identity: the
          // pane must keep its frozen `key` or React would remount it and tear down the socket
          // of a session that just launched (#127). What changes is the record's `actionKey`.
          onReconcileId={(sid) => onReconcile(wkey, sid)}
          onRole={(r) => onRole(wkey, r)}
          onTermStatus={onTermStatus}
          ref={termRef}
          onOpenGallery={onOpenGallery}
          onSaveAsTemplate={onSaveAsTemplate}
          // #1109: the chrome bar IS the pane head here — the pane's own bar is suppressed and
          // its chips portal into the slot above. The drawer toggle is REAL now: it opens the
          // file drawer below, and a session still resolving its cwd keeps the VISIBLE
          // DISABLED trigger a cwd-less session gets (#783), rather than a dead chip.
          suppressHead
          headActionsSlot={actionsSlot}
          headOverflowRef={paneOverflowRef}
          headReservePx={factsW + CHROME_RESERVE}
          headBarRef={headRef}
          filesOpen={filesOpen}
          onToggleFiles={(trigger?: HTMLElement | null) => {
            setFilesTrigger(trigger ?? null);
            setFilesOpen((open) => !open);
          }}
          filesDisabledReason={
            cwd ? undefined : "This session has not reported a folder yet"
          }
        />
        </RuntimeGate>
        {filesOpen && cwd && (
          <div
            className={filesMode === "dock" ? styles.drawerDock : styles.drawerSheet}
            data-window-files-drawer={filesMode}
          >
            <FilePanel
              // MOUNT identity is the WINDOW (wkey), never the action id (Hermes on #1109):
              // a fresh launch's `new-` placeholder reconciles to the server id mid-life, and
              // a key that follows that AUTOMATIC transition would unmount the panel — and a
              // dirty editor with it. The panel's SERVER identity travels separately, as the
              // `sessionKey` PROP: it re-points the API calls to the converged id without
              // touching the mounted component. The dock↔sheet flip below is also NOT a
              // remount: the panel stays mounted across it, so an open viewer and its tree
              // state survive a window resize across the boundary.
              key={wkey}
              sessionKey={actionKey}
              // A BACKGROUND window's sheet owns no keys: focus is per-window on the map,
              // and Escape/Tab belong to whichever window is active (Hermes on #1109).
              sheetKeyScopeActive={focused}
              cwd={cwd}
              paneWidth={bodyW}
              persistOpen={false}
              contained={filesMode === "sheet"}
              returnFocusTo={filesTrigger}
              onClose={() => setFilesOpen(false)}
              onSendPath={(path) => {
                // The panel knows the path; Compose knows the draft; neither knows the other.
                // Same handoff the full-screen pane makes (#792), through this pane's handle.
                termRef.current?.insertToken(pathToken(path, cwd));
              }}
            />
          </div>
        )}

      </div>
      <button
        type="button"
        className={styles.resize}
        aria-label="Resize window"
        title="Resize window (arrow keys)"
        onPointerDown={(e) => startGesture(e, "resize")}
        onKeyDown={onResizeKey}
        data-window-resize
      />
    </section>
  );
});
