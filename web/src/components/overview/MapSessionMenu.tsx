import { useEffect, useState } from "react";
import type { Session } from "../../types/api";
import {
  type SessionMenuHandlers,
  useSessionMenu,
} from "../sessions/useSessionMenu";
import { type MenuAnchor, MenuPopover } from "../sidebar/RowMenu";
import { SessionTextDialog } from "./SessionTextDialog";

/** The Overview map's ONE session menu (#968) — the sidebar row's menu (`useSessionMenu`), opened
 *  from a chip's ⋯, a right-click, or a window's chrome.
 *
 *  The canvas mounts this while a target is set and unmounts it on `onDone`. The host outlives the
 *  popover on purpose: an item like Session brief closes the menu and opens a dialog in the same
 *  press, and that dialog's state lives in this component — unmounting on menu-close would take
 *  the dialog with it. So `onDone` fires only once the menu is closed, no dialog is open, no
 *  rename/tag dialog is up, and no mutation is still running. */
export function MapSessionMenu({
  session,
  anchor,
  handlers,
  onMenuClose,
  onDone,
  reviewInFlight,
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
          items={menu.items}
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
