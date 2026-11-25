/** The Library's export and import entry points (#2144): a card's Export, the grid's Import from
 *  file tile and a detail's Export / Import… all open today's share dialogs, wired the one way
 *  (`ShareExportDialog` with the item preselected; `ShareImportDialog` with the shared import
 *  memory and the page's refresh). No API change. */
import { useState } from "react";
import type { ShareKind } from "../../types/share";
import { ShareExportDialog, ShareImportDialog } from "./ShareDialogs";

/** Import from a file. `onImported` runs when the operator closes results that created something,
 *  so the page re-reads its list (and anything marking imported copies) without a reload. */
export function useImportEntry(onImported: () => void) {
  const [open, setOpen] = useState(false);
  return {
    open: () => setOpen(true),
    dialog: open ? (
      <ShareImportDialog onClose={() => setOpen(false)} onImported={onImported} />
    ) : null,
  };
}

/** Export one item: the dialog opens with `<kind>:<id>` chosen (what it needs auto-ticks and locks). */
export function useExportEntry(kind: ShareKind) {
  const [item, setItem] = useState<string | null>(null);
  return {
    open: (id: string) => setItem(`${kind}:${id}`),
    dialog: item ? (
      <ShareExportDialog kind={kind} preselect={item} onClose={() => setItem(null)} />
    ) : null,
  };
}
