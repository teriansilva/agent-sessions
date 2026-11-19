import { useCallback, useRef, useState } from "react";
import type { Session } from "../../types/api";
import type { RowMenuEntry } from "../sidebar/RowMenu";
import { type HeadAction, HeadActions, type HeadActionsProps } from "../terminal/HeadActions";
import { omittedSessionKeys, paneMenuEntries } from "./paneMenu";
import { SessionMenuHost } from "./SessionMenuHost";
import { usePaneSessionHandlers } from "./usePaneSessionHandlers";

/** The names the pane's trigger and its menu have always had, by header form (#948 P6). */
const COLLAPSED_LABEL = "Actions for this session";
const FOLD_LABEL = "More session actions";

interface MenuTarget {
  /** The trigger, while the popover is open; `null` once it closed (a dialog may still be up). */
  anchor: HTMLElement | null;
  /** The row captured at open: a dialog already open keeps naming its session if the row is
   *  lost under it (the list dropped it and the lookup has not answered yet). */
  row: Session;
  paneItems: RowMenuEntry[];
  omit: ReadonlySet<string>;
}

/** The session pane's own head actions and its ONE menu (#2173): `HeadActions` in the pane's bar,
 *  with the "…" (the "Actions" trigger at the compact width) opening the SAME merged menu a map
 *  window's chrome opens: the Session group (`useSessionMenu`), then the Pane group, the head
 *  actions the fold kept off the bar. Both the terminal pane and the API pane render their in-pane
 *  bar through this, so the two cannot differ. A pane inside a map window does not: the window's
 *  chrome already owns its one menu (`foldInto: "external"`).
 *
 *  - **`row` unknown** (a deep link still resolving, a `new-` placeholder before its converge):
 *    there is no session to act on, so this is plain `HeadActions`: the pane-only overflow menu,
 *    present only when something folded.
 *  - **`row` known**: the trigger is always on the bar. The Pane group and the omitted session
 *    twins (`paneMenu`) are read from the fold at open time, as the window's chrome reads them.
 *
 *  The host (`SessionMenuHost`) outlives the popover: it stays mounted while a dialog it opened
 *  is up, a rename/tag dialog is open, or a mutation runs, and focus returns to the trigger, which
 *  never unmounts while the row is known. The menu is ANCHORED at every width: the pane's own
 *  menus are the one exception to the ≤800px bottom sheet (docs/design.md §5). */
export function PaneHeadActions({
  row,
  ...head
}: Omit<HeadActionsProps, "foldInto" | "overflowRef" | "allRef" | "merged"> & {
  row: Session | undefined;
}) {
  const overflowRef = useRef<HeadAction[]>([]);
  const allRef = useRef<HeadAction[]>([]);
  const triggerRef = useRef<HTMLElement | null>(null);
  const [target, setTarget] = useState<MenuTarget | null>(null);

  const toggle = (trigger: HTMLElement) => {
    triggerRef.current = trigger;
    if (target?.anchor) {
      setTarget({ ...target, anchor: null });
      return;
    }
    if (!row) return;
    setTarget({
      anchor: trigger,
      row,
      paneItems: paneMenuEntries(overflowRef.current, trigger),
      omit: omittedSessionKeys(allRef.current),
    });
  };
  const onMenuClose = useCallback((refocus: boolean) => {
    setTarget((t) => (t ? { ...t, anchor: null } : t));
    if (refocus) triggerRef.current?.focus();
  }, []);
  const onDone = useCallback(() => setTarget(null), []);

  return (
    <>
      <HeadActions
        {...head}
        overflowRef={overflowRef}
        allRef={allRef}
        merged={row ? { expanded: Boolean(target?.anchor), toggle } : undefined}
      />
      {target && (
        <PaneSessionMenu
          session={row ?? target.row}
          target={target}
          label={head.collapsed ? COLLAPSED_LABEL : FOLD_LABEL}
          triggerRef={triggerRef}
          onMenuClose={onMenuClose}
          onDone={onDone}
        />
      )}
    </>
  );
}

/** The mounted menu: its own component so the handlers (and their hooks) exist only while a menu
 *  or one of its dialogs does. */
function PaneSessionMenu({
  session,
  target,
  label,
  triggerRef,
  onMenuClose,
  onDone,
}: {
  session: Session;
  target: MenuTarget;
  label: string;
  triggerRef: React.RefObject<HTMLElement | null>;
  onMenuClose: (refocus: boolean) => void;
  onDone: () => void;
}) {
  const handlers = usePaneSessionHandlers(session);
  return (
    <SessionMenuHost
      session={session}
      anchor={target.anchor ? { element: target.anchor } : null}
      handlers={handlers}
      onMenuClose={onMenuClose}
      onDone={onDone}
      // A review started here keeps this host mounted until it settles (`reviewing`), so there is
      // no second host that could start another one for the same session.
      reviewInFlight={false}
      paneItems={target.paneItems}
      omit={target.omit}
      label={label}
      placement="anchored"
      ownerRef={triggerRef}
    />
  );
}
