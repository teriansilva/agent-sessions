/** The mission's secondary lifecycle, behind one `⋯` (#942).
 *
 *  The console used to render four lifecycle controls side by side at near-equal weight, two of
 *  them destructive and red. That is four shouts and no primary: the operator's eye has nowhere to
 *  land, and the two most dangerous actions compete with the one they almost always want.
 *
 *  So the state's own next step stays inline and everything else collapses here. Nothing is
 *  removed and nothing loses its confirmation — the two-tap CONFIRM path each destructive action
 *  already owns is unchanged, and every button keeps its `data-testid` and its label. Only
 *  prominence changes.
 *
 *  **It is a real menu, not a div that appears.** `role="menu"`, focus moved in, Escape closes,
 *  focus returns to the trigger, and a press outside dismisses — supplied by `useModalDrawer`,
 *  the same hook the notification bell and the shell drawer use. Writing a fifth ad-hoc popover
 *  is how a codebase ends up with five subtly different ones.
 *
 *  **And `role="menu"` is a promise about the ARROW KEYS**, not just a label: the pattern says
 *  Up/Down move between items, Home/End jump to the ends, and the menu is ONE tab stop rather
 *  than N. Claiming the role while leaving Tab to walk the items is the kind of ARIA that reads
 *  correct in the tree and behaves wrong under a screen reader, so the roving-focus handler
 *  below is part of the role, not decoration. Disabled items are skipped, which is why the query
 *  filters them rather than indexing blindly.
 *
 *  **And Tab LEAVES.** That takes two things, not one: the handler below (which closes on Tab and
 *  lets `useModalDrawer`'s own restore put focus back on `⋯`, outside the menu), and
 *  `containFocus: false`. The second is not implied by the first, and it is not implied by an
 *  empty `inertRefs` either — the hook installs its Tab-containment listener independently of the
 *  isolation, so "isolate nothing" alone would leave this menu carrying a modal focus trap while
 *  claiming to be non-modal. The flag is what stops the hook fighting the handler for focus, and
 *  what stops the NEXT menu from inheriting a trap by writing no Tab handler at all.
 *
 *  It is deliberately NOT `aria-modal`: this is a small anchored menu over a page that stays
 *  usable, not a surface that owns the screen, and claiming otherwise would tell a screen reader
 *  the console had gone away.
 */
import { useRef, useState } from "react";

import styles from "./mission.module.css";
import { useModalDrawer } from "./useModalDrawer";

export function MissionOverflow({
  busy,
  children,
}: {
  busy?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const firstRef = useRef<HTMLDivElement | null>(null);

  /** The menu's own items, in DOM order, minus the ones that cannot be actioned. Read from the
   *  DOM rather than from a list prop because WHICH items exist is decided by the mission's
   *  state in `MissionLifecycle` — a parallel list here would be a second copy of that map, free
   *  to drift from the one that renders. */
  function items(): HTMLElement[] {
    const el = panelRef.current;
    if (!el) return [];
    return Array.from(
      el.querySelectorAll<HTMLElement>('[role="menuitem"]'),
    ).filter((n) => !n.hasAttribute("disabled"));
  }

  function onKeyDown(e: React.KeyboardEvent) {
    // TAB LEAVES, it does not wrap (#942 review). A menu is not a modal: the page behind it is
    // live, so tabbing away is a legitimate thing for the operator to do and the menu's job is to
    // get out of the way when they do. It closes, and `useModalDrawer`'s own restore puts focus
    // back on `⋯` — outside the menu, on the control the menu belongs to, from which the next Tab
    // continues into the page. Both directions, because a trap that holds one way is still a trap.
    if (e.key === "Tab") {
      e.preventDefault();
      setOpen(false);
      return;
    }
    const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
    if (!keys.includes(e.key)) return;
    const list = items();
    if (!list.length) return;
    e.preventDefault();
    // `document.activeElement` may be the wrapper we focused on open, which is not in the list —
    // `indexOf` then answers -1, and -1 + 1 is 0, so Down lands on the first item. That is the
    // behaviour the pattern asks for, and it falls out rather than needing a branch.
    const at = list.indexOf(document.activeElement as HTMLElement);
    const next =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? list.length - 1
          : e.key === "ArrowDown"
            ? (at + 1) % list.length
            : (at <= 0 ? list.length : at) - 1;
    list[next]?.focus();
  }

  useModalDrawer({
    active: open,
    panelRef,
    initialFocusRef: firstRef,
    triggerRef,
    onClose: () => setOpen(false),
    // THE TRIGGER IS INSIDE, so it can close what it opened (#942 review 3). Without this the
    // hook read a mousedown on `⋯` as an outside press and closed the menu, and the click that
    // followed the same press toggled `open` back on — so two real clicks left the menu OPEN.
    // A synthetic `click()` hides this completely: it dispatches no mousedown.
    insideRefs: [triggerRef],
    // The background stays live — see the note above. Naming an empty set is what says "this
    // panel isolates nothing", rather than inheriting the default that makes `#root` inert.
    inertRefs: [],
    // …AND FOCUS IS NOT CONTAINED, which is a SECOND thing and not implied by the first (#942
    // review). The hook installs its Tab cycle independently of the isolation, so an empty
    // `inertRefs` alone left this menu with nothing inert and Tab wrapping forever inside it —
    // a non-modal surface carrying a modal trap. Tab is handled below instead: it closes.
    containFocus: false,
  });

  return (
    <span className={styles.overflowWrap}>
      <button
        type="button"
        ref={triggerRef}
        className={styles.missionBtn}
        disabled={busy}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="More mission actions"
        onClick={() => setOpen((v) => !v)}
        data-testid="mission-overflow"
      >
        ⋯
      </button>
      {open ? (
        <div
          ref={panelRef}
          className={styles.overflowMenu}
          role="menu"
          aria-label="More mission actions"
          data-testid="mission-overflow-menu"
          onKeyDown={onKeyDown}
        >
          {/* The focus target on open. A wrapper rather than the first button, because which
              button is first depends on the mission's state — and focusing "whatever happens to
              be first" would land on a destructive action in some states and not others. */}
          <div ref={firstRef} tabIndex={-1} className={styles.overflowItems}>
            {children}
          </div>
        </div>
      ) : null}
    </span>
  );
}
