import { Maximize2, X } from "lucide-react";
import { memo, type PointerEvent as ReactPointerEvent, useRef, useState } from "react";
import { engineBadge, engineName } from "../../lib/format";
import type { TermRole } from "../../lib/termSocket";
import { Terminal } from "../terminal/Terminal";
import styles from "./sessionWindow.module.css";
import { clampRect, type Rect, type Size } from "./workspace";

/** One floating session window on the Overview map (#208).
 *
 *  A window is the EXISTING session pane with a 30px bar on top — `<Terminal>` is mounted with
 *  the same props the `/s/:engine/:id` route gives it, so there is no second terminal
 *  implementation to keep in step. Everything this component adds is chrome: drag, resize,
 *  focus/raise, and the two controls.
 *
 *  Two deliberate non-features:
 *  - **It never calls `fit()`.** Changing the window's CSS size is enough; the pane's own
 *    ResizeObserver → debounced `refitSoon` (#859) does the refit, which is what stops a drag
 *    from marching the agent through every intermediate width (the #227/#349 resize storm).
 *  - **It never touches the socket.** Mount/unmount is the whole lifecycle: closing the window
 *    unmounts `<Terminal>`, which tears its own socket and document-level listeners down. */
export const SessionWindow = memo(function SessionWindow({
  wkey,
  engine,
  id,
  title,
  rect,
  bounds,
  focused,
  role,
  onFocus,
  onClose,
  onFullScreen,
  onRect,
  onRole,
}: {
  /** The engine-qualified session id. Passed back to every handler so the handlers themselves
   *  can be the workspace's own stable callbacks rather than per-render closures — that is what
   *  lets `memo` above actually hold, and a mounted terminal is an expensive thing to re-render
   *  because the map moved. */
  wkey: string;
  engine: string;
  id: string;
  title: string;
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
}) {
  const [dragging, setDragging] = useState(false);
  // Pointer origin + the rect at gesture start. A ref, not state: it is read inside the move
  // handler and must never re-render on its own.
  const gestureRef = useRef<{ px: number; py: number; rect: Rect } | null>(null);

  const startGesture = (
    e: ReactPointerEvent<HTMLElement>,
    kind: "move" | "resize",
  ) => {
    // The header is the only drag initiator: a press that lands on ⤢, ✕ or the grip must not
    // start a window drag, or every click on a control would nudge the window first.
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
      style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
      // Capture, so raising happens before the terminal swallows the press for selection.
      onPointerDownCapture={() => onFocus(wkey)}
      aria-label={`Session window: ${title}`}
      data-session-window={`${engine}:${id}`}
      data-focused={focused ? "true" : "false"}
    >
      <div
        className={styles.head}
        onPointerDown={(e) => startGesture(e, "move")}
        data-window-head
      >
        <span className={styles.grip} aria-hidden="true">
          ⣿
        </span>
        <span className={styles.eng} title={engineName(engine)}>
          {engineBadge(engine)}
        </span>
        {/* The pane's own header deliberately omits the title (it is the sidebar's job there);
            a floating window has no sidebar beside it, so the chrome carries it. */}
        <span className={styles.title} title={title}>
          {title}
        </span>
        {role === "secondary" && (
          <span className={styles.lock} data-window-readonly>
            READ-ONLY
          </span>
        )}
        <span className={styles.btns}>
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
      <div className={styles.body}>
        <Terminal
          engine={engine}
          id={id}
          onRole={(r) => onRole(wkey, r)}
          // Decision 3 (#208): the Files panel is SessionView's sibling pane, not part of the
          // terminal, so it does not come into a window. The trigger stays VISIBLE and disabled
          // with the reason — the same treatment a cwd-less session gets (#783) — rather than
          // vanishing, which would read as a glitch.
          onToggleFiles={() => {}}
          filesDisabledReason="Open this session full screen to browse its files"
        />
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
