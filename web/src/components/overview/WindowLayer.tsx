import { type Node, useReactFlow, useStore } from "@xyflow/react";
import { type RefObject, useCallback, useMemo } from "react";
import type { TermRole } from "../../lib/termSocket";
import type { MenuAnchor } from "../sidebar/RowMenu";
import { anchorPointOf } from "./nodeAnchor";
import { SessionWindow } from "./SessionWindow";
import styles from "./windowLayer.module.css";
import type { WorkspaceWindow } from "./useWorkspace";
import {
  clampRect,
  clipToBounds,
  type Point,
  type Rect,
  type Size,
  tetherAnchor,
  tetherPath,
} from "./workspace";

/** Why a window's session items are unavailable: its menu reads the session's row from the map,
 *  and the row is not there (#968). #1109 refines this from "the whole ⋯ is disabled" to "only
 *  the row-dependent session actions are": the ⋯ stays enabled and opens the PANE actions only
 *  (its own fold — Repaint, Files, text size), which never needed the row. The reason is shown
 *  as the control's title, so the state explains itself. */
const OFF_MAP_REASON =
  "This session isn't on the map right now — change the map filter or open it full screen";

/** The floating window layer over the map (#208).
 *
 *  It is a SIBLING of `<ReactFlow>`, not a node inside it, and that is the whole architecture:
 *  React Flow zooms by putting a CSS `transform: scale()` on its viewport, and xterm sizes a
 *  canvas in device pixels and maps mouse→cell off the canvas rect — so a scaled ancestor makes
 *  the terminal blurry and breaks fit/selection/input maths. Out here nothing scales the pane;
 *  only the tether is re-projected as the map moves.
 *
 *  Coordinates are **overlay-local**. `flowToScreenPosition` hands back viewport-absolute
 *  screen coordinates, and the overlay sits inside the app shell (sidebar, header, toolbar), so
 *  its origin is never the viewport's — subtracting `originX/originY` is what makes a tether
 *  land on its chip at any shell offset, not only at the origin. */
export function WindowLayer({
  layerRef,
  windows,
  focusedKey,
  flashKey,
  notice,
  bounds,
  originX,
  originY,
  topInset,
  anchorCandidates,
  onFocus,
  onMinimize,
  onClose,
  onFullScreen,
  onRect,
  onRole,
  onReconcile,
  onMenu,
  isOnMap,
}: {
  layerRef: RefObject<HTMLDivElement | null>;
  windows: WorkspaceWindow[];
  focusedKey: string | null;
  flashKey: string | null;
  notice: string | null;
  bounds: Size;
  /** The overlay box's top-left in SCREEN coordinates — the shell offset to subtract. */
  originX: number;
  originY: number;
  /** How far below the wrapper's top the overlay starts (clears the map toolbar). */
  topInset: number;
  /** Node ids that could anchor a window's tether, best first: the session chip, then the
   *  cluster it collapses into. */
  anchorCandidates: (sessionKey: string) => string[];
  onFocus: (key: string) => void;
  /** Park a window in the tray (hidden, still live). Absent → the chrome carries no –. */
  onMinimize?: (key: string) => void;
  onClose: (key: string) => void;
  onFullScreen: (key: string) => void;
  onRect: (key: string, rect: Rect) => void;
  onRole: (key: string, role: TermRole) => void;
  /** A window launched under a `new-<uuid>` placeholder adopting the id its engine minted. */
  onReconcile: (key: string, sid: string) => void;
  /** Open the session menu for a window (#968). Absent → the chrome carries no ⋯. */
  onMenu?: (key: string, anchor: MenuAnchor, opener: HTMLElement | null) => void;
  /** Is this session (by `actionKey`) on the map, where its menu reads the row from? */
  isOnMap?: (sessionKey: string) => boolean;
}) {
  const { flowToScreenPosition } = useReactFlow();
  // Re-project on every pan/zoom. The store's transform is a stable reference that changes only
  // when the viewport does, so this subscribes without a render loop.
  const transform = useStore((s) => s.transform);
  // ...and on every relayout. This subscribes to the node ARRAY, not to `s.nodes.length`, which
  // was the first cut and is not enough: switching the grouping mode rebuilds the graph with a
  // DIFFERENT layout but often the SAME node count (two projects → two engines), so no
  // subscribed value changed, nothing recomputed, and every tether stayed at its old
  // coordinates until an unrelated pan or zoom happened to wake the layer up. React Flow
  // replaces this array whenever nodes are set or moved, so its identity is the honest signal —
  // which is also what keeps a tether on its cluster while that cluster is dragged (#968).
  const nodes = useStore((s) => s.nodes);
  // Look anchors up in the SAME snapshot we are subscribed to, rather than through
  // `getNode` — that reads the live store, so a projection could mix a fresh position with a
  // stale render and be wrong in a way nothing re-ran to correct.
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const lookup = useCallback((id: string) => byId.get(id), [byId]);

  const tethers = useMemo(() => {
    const out: { key: string; d: string; from: Point; focused: boolean }[] = [];
    for (const w of windows) {
      // A parked window has nothing on screen to point at.
      if (w.minimized) continue;
      // First candidate that is actually on the map wins: the chip, else the cluster it
      // collapsed into. Neither present (the chip was filtered off the map, the session was
      // archived, the grouping changed) → no tether at all, and the window stays open and
      // fully usable. A map filter never closes a window.
      // `actionKey`, not `key`: a window still transporting on a `new-` placeholder has no chip
      // until its engine reconciles, and the chip it gets then carries the real id (#936/#867).
      const node = anchorCandidates(w.actionKey)
        .map((id) => lookup(id))
        .find((n): n is Node => !!n);
      if (!node) continue;
      // Chip edge, vertically centred, projected flow → screen → overlay-local.
      const from = clipToBounds(
        anchorPointOf(node, lookup, flowToScreenPosition, {
          x: originX,
          y: originY,
        }),
        bounds,
      );
      const to = tetherAnchor(clampRect(w.rect, bounds), from);
      out.push({ key: w.key, d: tetherPath(from, to), from, focused: w.key === focusedKey });
    }
    return out;
    // `transform` is a REPROJECTION TRIGGER the lint rule cannot see: it is read indirectly, by
    // `flowToScreenPosition` reaching into React Flow's store. Dropping it freezes every tether
    // through pan and zoom. (`lookup` carries the node snapshot, so relayouts are a real dep.)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    windows,
    focusedKey,
    anchorCandidates,
    lookup,
    flowToScreenPosition,
    originX,
    originY,
    bounds,
    transform,
  ]);

  return (
    <div
      ref={layerRef}
      className={styles.layer}
      style={{ top: topInset }}
      data-window-layer
      data-window-count={windows.length}
    >
      {/* Tethers paint under the windows: a line should never cross the pane it points at. */}
      <svg
        className={styles.tethers}
        width={bounds.w}
        height={bounds.h}
        aria-hidden="true"
      >
        {tethers.map((t) => (
          <g key={t.key} data-tether={t.key} data-tether-d={t.d}>
            <path
              className={t.focused ? styles.lineFocused : styles.line}
              d={t.d}
            />
            <circle className={styles.dot} cx={t.from.x} cy={t.from.y} r={3.5} />
          </g>
        ))}
      </svg>
      {windows.map((w) => (
        <div
          key={w.key}
          // A parked window stays MOUNTED (unmounting closes its socket) and keeps its layout box:
          // `visibility: hidden` leaves the pane at its size, so restoring it needs no refit.
          className={`${styles.slot}${flashKey === w.key ? ` ${styles.flash}` : ""}${w.minimized ? ` ${styles.parked}` : ""}`}
          style={{ zIndex: w.z }}
          aria-hidden={w.minimized ? true : undefined}
          data-window-minimized={w.minimized ? "true" : undefined}
        >
          <SessionWindow
            wkey={w.key}
            engine={w.engine}
            id={w.id}
            actionKey={w.actionKey}
            title={w.title}
            fresh={w.fresh}
            // Fitted to the box as it is NOW; `w.rect` stays the operator's intent, so a map
            // that shrinks and grows returns the window to the layout it had.
            rect={clampRect(w.rect, bounds)}
            bounds={bounds}
            focused={focusedKey === w.key}
            role={w.role}
            onFocus={onFocus}
            onMinimize={onMinimize}
            onClose={onClose}
            onFullScreen={onFullScreen}
            onRect={onRect}
            onRole={onRole}
            onReconcile={onReconcile}
            onMenu={onMenu}
            // A primitive, so a refetch that leaves this window's session on the map re-renders
            // nothing inside the memoized window.
            offMapReason={
              onMenu && isOnMap && !isOnMap(w.actionKey) ? OFF_MAP_REASON : undefined
            }
          />
        </div>
      ))}
      {/* The tray: one chip per parked window, in the order they were opened. A click raises it,
          which un-parks it (the reducer's `raise`). */}
      {windows.some((w) => w.minimized) && (
        <div className={styles.tray} role="toolbar" aria-label="Minimized windows" data-window-tray>
          {windows
            .filter((w) => w.minimized)
            .map((w) => (
              <button
                key={w.key}
                type="button"
                className={styles.trayChip}
                onClick={() => onFocus(w.key)}
                title={`Restore ${w.title}`}
                aria-label={`Restore window: ${w.title}`}
                data-window-tray-chip={w.actionKey}
              >
                <span className={styles.trayDot} aria-hidden="true" />
                <span className={styles.trayTitle}>{w.title}</span>
              </button>
            ))}
        </div>
      )}
      {notice && (
        <div className={styles.notice} role="status" data-window-notice>
          {notice}
        </div>
      )}
    </div>
  );
}
