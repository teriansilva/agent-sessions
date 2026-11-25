/** Who owns Escape while HQ works (#2076). Stop takes it only when nothing is open OVER the
 *  console: a dialog, a sheet, a list or a menu answers its own Escape first. A surface HQ itself
 *  sits in (the phone drawer is a dialog) is not one over it. Its own module so the console stays
 *  free of popup markup. */
const SURFACES =
  'dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"], [role="listbox"], [role="menu"]';

export function surfaceOver(root: Element): boolean {
  return Array.from(document.querySelectorAll(SURFACES)).some((el) => !el.contains(root));
}
