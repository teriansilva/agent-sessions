import { useEffect, useState } from "react";
import type { Session } from "../../types/api";
import {
  type SessionMenuHandlers,
  useSessionMenu,
} from "../sessions/useSessionMenu";
import {
  type RowMenuEntry,
  type MenuAnchor,
  MenuPopover,
} from "../sidebar/RowMenu";
import { SessionTextDialog } from "./SessionTextDialog";

/** The Overview map's ONE session menu (#968) — the sidebar row's menu (`useSessionMenu`), opened
 *  from a chip's ⋯, a right-click, or a window's chrome.
 *
 *  The canvas mounts this while a target is set and unmounts it on `onDone`. The host outlives the
 *  popover on purpose: an item like Session brief closes the menu and opens a dialog in the same
 *  press, and that dialog's state lives in this component — unmounting on menu-close would take
 *  the dialog with it. So `onDone` fires only once the menu is closed, no dialog is open, no
 *  rename/tag dialog is up, and no mutation is still running.
 *
 *  A WINDOW's ⋯ opens this same menu with `paneItems` (#1109): the pane head actions its chips'
 *  measured fold could not fit, appended as a labelled second group after the session items —
 *  one menu per window, never two. The session group keeps `useSessionMenu`'s items verbatim
 *  (the list that must not fork); the pane group is the host's own fold, deduped BY THE HOST
 *  against the session group so one modal is never named twice. */
export function MapSessionMenu({
  session,
  anchor,
  handlers,
  onMenuClose,
  onDone,
  reviewInFlight,
  paneItems,
}: {
  /** The live row while the session is on the map, else the row captured when the menu opened —
   *  a dialog already open keeps naming its session after a refetch drops the chip. */
  session: Session;
  /** `null` once the popover has closed (a dialog may still be open). */
  anchor: MenuAnchor | null;
  handlers: Omit<SessionMenuHandlers, "onEdit"> & {
    onRename: (id: string, title: string) => Promise<void>;
    onSetTag: (id: string, tag: string) => Promise<void>;
  };
  onMenuClose: (refocus: boolean) => void;
  onDone: () => void;
  /** A review of this session is running — tracked by the canvas per session key, so it survives
   *  this host closing, or being replaced by another session's menu (#968 review). */
  reviewInFlight: boolean;
  /** The window's folded pane actions (#1109), already converted to menu entries and deduped
   *  against the session group by the host. Chips that fit never appear here. */
  paneItems?: RowMenuEntry[];
}) {
  const [editMode, setEditMode] = useState<"none" | "title" | "tag">("none");
  const [editReturnFocus, setEditReturnFocus] = useState<HTMLElement | null>(null);
  const menu = useSessionMenu(
    session,
    {
      ...handlers,
      onEdit: (mode) => {
        setEditReturnFocus(document.activeElement as HTMLElement | null);
        setEditMode(mode);
      },
    },
    { reviewInFlight },
  );
  const { busy, dialogOpen, reviewing, runBusy } = menu;

  // The merged item list: the session group first (labelled), then the window's pane group.
  // The session items' own `pushGroup` separators stand; a leading separator is never added,
  // and an empty pane group adds nothing at all.
  const items: RowMenuEntry[] = [{ group: "Session" }, ...menu.items];
  if (paneItems?.length) {
    items.push("separator", { group: "Pane" }, ...paneItems);
  }

  const idle =
    !anchor && !dialogOpen && editMode === "none" && !busy && !reviewing;
  useEffect(() => {
    if (idle) onDone();
  }, [idle, onDone]);

  const saveText = (value: string) => {
    const mode = editMode;
    setEditMode("none");
    if (mode === "tag") {
      // Empty IS valid for a tag — it clears it; skip the write only when unchanged.
      if (value === (session.tag ?? "")) return;
      void runBusy(() => handlers.onSetTag(session.id, value));
    } else if (value && value !== session.title) {
      // An empty title is a no-op, exactly as in the sidebar's inline editor.
      void runBusy(() => handlers.onRename(session.id, value));
    }
  };

  return (
    <>
      {anchor && (
        <MenuPopover
          items={items}
          title={session.title || session.short_uuid}
          anchor={anchor}
          onClose={onMenuClose}
        />
      )}
      {menu.dialogs}
      {editMode !== "none" && (
        <SessionTextDialog
          mode={editMode}
          sessionTitle={session.title || session.short_uuid}
          initial={editMode === "tag" ? (session.tag ?? "") : session.title}
          onCancel={() => setEditMode("none")}
          onSave={saveText}
          returnFocusTo={editReturnFocus}
        />
      )}
    </>
  );
}
