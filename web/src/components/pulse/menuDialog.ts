/** Open a dialog from a mission ⋯ menu item, in the one order that leaves the page inert and focus
 *  inside the dialog (#1968, #1969's independent review).
 *
 *  A desktop panel stays open when its item is clicked, so a dialog over it would leave the menu
 *  behind a modal. Close it through its own trigger first: the trigger's click is what `AnchoredMenu`
 *  listens for, and it also lets the menu restore focus before the dialog takes it.
 *
 *  A phone's ⋯ menu is a MODAL bottom sheet: it already holds `inert` on `#root` and closes on a
 *  microtask after the item's handler, lifting its own mark as it goes. A dialog mounted in the
 *  same tick therefore sees an already-inert root and declines to own it (`useInertBehind`'s
 *  nested-dialog guard) — then the sheet's cleanup removes the only mark left. Deferring the open
 *  one task past that close makes the dialog the owner: it sets `inert`, takes focus, and the
 *  sheet's restore can no longer win the race.
 *
 *  The dialog needs no user activation (no file picker, no clipboard), so deferring is safe.
 *  Shared by every item in this menu that opens a dialog — Save as mission plan and Artifacts… —
 *  so the two cannot drift. */

/** `item` is the clicked menu item (React's `event.currentTarget`). `open` receives the ⋯ trigger
 *  the dialog returns focus to. */
export function openDialogFromMenuItem(
  item: HTMLElement,
  open: (trigger: HTMLButtonElement | null) => void,
): void {
  // The ⋯ trigger of THIS header (not the first one in the document): focus returns to it.
  // A phone's sheet is portalled to <body>, outside the header: then the one console's.
  const trigger =
    (item.closest('[data-testid="mission-lifecycle"]') ?? document)
      .querySelector<HTMLButtonElement>('[data-testid="mission-overflow"]') ?? null;
  const inSheet = !!item.closest("[data-sheet]");
  if (trigger?.getAttribute("aria-expanded") === "true" && !inSheet) trigger.click();
  if (inSheet) setTimeout(() => open(trigger), 0);
  else open(trigger);
}
