/** The modal-drawer contract, in one place (#878).
 *
 * Extracted verbatim from `NotificationBell`, which implemented it inline and correctly. It is
 * shared rather than copied because a focus trap is exactly the kind of thing that rots when
 * duplicated: the second copy is written from memory of the first, drifts, and the drift is
 * invisible until an operator is trapped in a panel. `mobile-pulse-layout.spec.ts` exercises the
 * bell's drawer end-to-end and is the safety net for this extraction — if the hook is not
 * faithful, those tests say so.
 *
 * `aria-modal` is a PROMISE, and declaring it without honouring it is worse than not declaring
 * it at all: it tells assistive technology the background is inert while it is still reachable.
 * The promise has four parts, and all four live here:
 *
 *   1. focus moves INTO the panel on open;
 *   2. `#root` is made genuinely `inert` — the panel and scrim are portalled OUTSIDE `#root`,
 *      so the background is isolated without touching either;
 *   3. Tab is contained, which is what makes the trap observable in a test rather than inferred
 *      from an attribute. It has two exceptions and they answer different questions: it stands
 *      down for a NESTED modal, because the topmost surface owns the keyboard
 *      (`nestedSurfaceHasFocus`), and it is opt-out entirely (`containFocus`) for a caller that
 *      is a MENU rather than a modal, because a menu's Tab must be able to leave;
 *   4. focus RETURNS to the trigger on every close path — Escape, scrim, an action inside, or a
 *      resize that drops drawer mode.
 */
import { useCallback, useEffect, useRef, type RefObject } from "react";

/** THE OPEN MODAL SURFACES, in the order they opened.
 *
 *  Ownership has to be answerable **without asking where focus is** (#940 review 4). The focus
 *  test below is necessary — it is the only thing that can recognise a surface which does not use
 *  this module at all, like the row menu's `[data-modal-inside]` sheet — but it is not sufficient,
 *  and the gap is not hypothetical: `MoveToProjectModal` moves focus to its first option only
 *  after `GET /api/projects` resolves. Hold that request, or fail it, and focus sits on the
 *  trigger outside the child for as long as the operator looks at the spinner or the error. One
 *  Escape then closed the DRAWER and left the dialog mounted inside it, parked and inert.
 *
 *  A dialog is open from the moment it mounts, whatever its data is doing, so registration is
 *  what says so. Mount order is stack order: the drawer registers first, the child it opens
 *  registers second, and "is anything above me" is an index comparison rather than a guess. */
const surfaceStack: object[] = [];

/** Is focus currently owned by a modal surface NESTED inside this one — a child dialog that this
 *  panel opened but does not contain?
 *
 *  This is the keyboard half of the portal problem `[data-modal-inside]` solves for the pointer
 *  (#940 review 2). The sidebar's Session-actions menu opens Session brief, Hand off and Move to
 *  project; the drawer's own Tab trap treated a press inside any of them as an escape and hauled
 *  focus back to its Close button. Measured at 412×900: open the drawer, Session actions, Session
 *  brief, press Tab — focus left the brief for the drawer while the brief stayed open. On `main`
 *  before the drawer became modal, the same press reached "Review now" inside the brief.
 *
 *  The rule is ownership: **while a nested modal holds focus, this panel is not the topmost
 *  surface and owes the keyboard nothing.** The child brings its own trap, its own Escape and its
 *  own restore; the parent standing down is what lets them work. It stands down for Escape too,
 *  which is what stops one press closing the child AND the drawer under it.
 *
 *  **DOM CONTAINMENT DOES NOT DESCRIBE THE MODAL STACK, in either direction** (#940 review 3).
 *  The first version of this also required `!host.contains(surface)`, on the theory that a dialog
 *  rendered inside the panel is the panel's business — and that theory was written from the
 *  assumption that all three children portal to `<body>`. Two do. **`MoveToProjectModal` is
 *  rendered directly in the sidebar row**, so the extra condition said "not nested" for the one
 *  child that is a DOM descendant, and a single Escape inside it closed the drawer beneath it and
 *  left the dialog mounted in a panel that was now parked and inert. Verified in Chromium at
 *  412×900 before this was corrected.
 *
 *  So the test is simply "a modal surface other than this panel has focus". Where it is mounted
 *  is a rendering detail; which surface is on top is the question. */
function nestedSurfaceHasFocus(host: HTMLElement, el: Element | null): boolean {
  if (!el) return false;
  // The NEAREST such surface, which is what makes `!== host` sufficient: a control that belongs to
  // the panel itself resolves to the panel (the drawer carries `role="dialog"` while it is modal),
  // and only a control inside something nested resolves to that something.
  const surface = el.closest(
    '[role="dialog"], [aria-modal="true"], [data-modal-inside]',
  );
  // …AND AN ANCESTOR IS NOT A CHILD (#940 review 5). This is asked from BOTH ends now: the drawer
  // asks it about its children, and a child asks it while recovering focus that has escaped — and
  // for the child, the nearest surface around the escaped element is the DRAWER, which contains
  // it. Reading that as "something newer is on top" would make the child stand down exactly when
  // it needs to act, and it would never get focus back.
  //
  // Note this is the opposite direction from the condition removed in review 3: a surface the
  // host CONTAINS may well be a child (Move to project is), while a surface that contains the
  // host cannot be.
  return !!surface && surface !== host && !surface.contains(host);
}

export interface ModalDrawerOptions {
  /** Whether the drawer is open AND in drawer mode. A panel that is merely an anchored dropdown
   *  owes none of this, so both conditions are the caller's to combine. */
  active: boolean;
  /** The panel element — the focus-containment host. */
  panelRef: RefObject<HTMLElement | null>;
  /** Where focus goes on open, and what it is contained around. Usually the Close button:
   *  `#root` goes inert, so the trigger itself stops being clickable while open, which is why
   *  the drawer must carry its own close control. */
  initialFocusRef: RefObject<HTMLElement | null>;
  /** Focus returns here on close. */
  triggerRef: RefObject<HTMLElement | null>;
  /** Escape and scrim/outside-click both call this. */
  onClose: () => void;
  /** Whether Tab is CONTAINED inside the panel. Default `true`, which is what `aria-modal`
   *  promises and what every drawer here wants.
   *
   *  A small anchored MENU wants the opposite, and "pass an empty `inertRefs`" does not buy it
   *  (#942 review): the containment effect below is installed independently of the isolation, so
   *  a caller that made nothing inert still had its Tab cycle wrapped — a non-modal surface with
   *  a modal trap in it, which is the worst of both. Tab must be able to LEAVE a menu; the menu
   *  handles the key itself and closes.
   *
   *  Separate from `inertRefs` on purpose. They answer different questions — "can the background
   *  be reached" and "can focus leave" — and a surface that is inert-free but focus-trapped is
   *  exactly the bug this flag exists to make impossible to write by omission. */
  containFocus?: boolean;
  /** Regions that count as "inside" for outside-click purposes. The panel is portalled to
   *  <body>, so testing a wrapper alone would close it on every tap of its own rows. */
  insideRefs?: RefObject<HTMLElement | null>[];
  /** What to make `inert` while the drawer is open. Defaults to `#root`.
   *
   *  The default assumes the panel is portalled OUT of `#root` — which is how `NotificationBell`
   *  and `MissionDrawer` use it, and why "make the whole app inert" is both correct and simple
   *  for them.
   *
   *  The app shell's sidebar cannot do that: it *is* a grid track of `.app`, inside `#root`, and
   *  moving it out would take its layout, its resize handle and its CSS context with it on every
   *  route (#940). Isolating the background is still owed — so instead of relocating the panel,
   *  the caller names the regions to isolate, and the panel simply is not among them.
   *
   *  Passing this makes the CALLER responsible for covering the background completely: anything
   *  it does not name stays reachable. That is a sharper edge than the default, which is why the
   *  default is what it is. */
  inertRefs?: RefObject<HTMLElement | null>[];
}

/** Register this surface for as long as it is open, and answer whether another opened after it.
 *
 *  The token is a per-instance object rather than the panel element: refs are null on the first
 *  effect run and can change identity, and the question here is about the COMPONENT's lifetime,
 *  which is exactly what a ref-held object marks. */
function useSurfaceStack(active: boolean): () => boolean {
  const token = useRef<object>({});
  useEffect(() => {
    if (!active) return;
    const mine = token.current;
    surfaceStack.push(mine);
    return () => {
      const i = surfaceStack.indexOf(mine);
      if (i >= 0) surfaceStack.splice(i, 1);
    };
  }, [active]);
  // STABLE, so the effects that consult it can name it as a dependency instead of omitting it
  // (#940 review 5, non-blocking). It closes over a ref and a module-level array and reads both at
  // call time, so there is no value to go stale — but "there is nothing to go stale" is a claim a
  // reader has to verify, while an empty dep list on a `useCallback` is one they can see.
  return useCallback(() => {
    const i = surfaceStack.indexOf(token.current);
    return i >= 0 && i < surfaceStack.length - 1;
  }, []);
}

export function useModalDrawer({
  active,
  panelRef,
  initialFocusRef,
  triggerRef,
  onClose,
  insideRefs = [],
  inertRefs,
  containFocus = true,
}: ModalDrawerOptions): void {
  // TWO TESTS, and each covers what the other cannot. The stack knows a child is open even while
  // focus is still outside it (a dialog waiting on a fetch); the focus test knows a surface that
  // never registered (the row menu's portalled sheet, which declares `[data-modal-inside]`).
  const hasSurfaceAbove = useSurfaceStack(active);

  // Close on outside click / Escape — a panel that traps the operator is worse than no panel.
  useEffect(() => {
    if (!active) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t)) return;
      for (const r of insideRefs) if (r.current?.contains(t)) return;
      // A PORTAL OPENED FROM INSIDE THE PANEL IS STILL INSIDE IT (#940 review 1).
      //
      // DOM containment is the wrong test for a React portal: the sidebar's own Session-actions
      // menu renders into `document.body`, so a press on "Rename" is not inside the panel by
      // `contains()` and read as a dismissal — closing the drawer on `mousedown`, before the
      // item's click had finished. The rename editor then mounted inside a panel that was already
      // sliding away, 81px off-screen.
      //
      // Ref-based `insideRefs` cannot express this either: the menu is mounted by a component
      // deep in the tree, and only while it is open. So the portal declares itself instead, and
      // any surface that escapes its parent can opt in the same way.
      if ((t as Element).closest?.("[data-modal-inside]")) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // A child dialog answers its own Escape — see `nestedSurfaceHasFocus` and `surfaceStack`.
      // Without this one press closes the child and the drawer beneath it, which is never what
      // was asked for.
      if (hasSurfaceAbove()) return;
      const host = panelRef.current;
      if (host && nestedSurfaceHasFocus(host, document.activeElement)) return;
      onClose();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
    // `insideRefs` is a fresh array each render by construction; its REFS are stable, and the
    // handler reads them at event time, so re-subscribing on identity would churn for nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, onClose, panelRef]);

  /** BACKGROUND ISOLATION, on its own lifetime (#942 review 2).
   *
   *  This used to share one effect with the focus move, and sharing was the bug. The focus effect
   *  moves focus IN on setup and queues the restore to the trigger on CLEANUP, so depending on
   *  `inertRefs` identity meant: any caller passing an array literal re-ran that whole cycle on
   *  every render of its own component. `MissionOverflow` passes `inertRefs: []`, so a
   *  confirmation re-render — MARK FAILED becoming CONFIRM MARK FAILED — tore the effect down and
   *  back up, and the queued `requestAnimationFrame` put focus on `⋯` while the menu was still
   *  open. The next Enter operated the trigger instead of confirming.
   *
   *  Splitting them is the honest fix rather than mirroring the prop into a ref: these are two
   *  different lifetimes that happened to be written together. Isolation depends on WHAT is
   *  isolated; focus depends on whether the panel is open. Now each says so.
   *
   *  DEFINED FIRST, deliberately: on unmount React runs cleanups in definition order, so `inert`
   *  comes off before the focus effect below restores the trigger — and the trigger lives inside
   *  the isolated region, where it could not take focus while inert. */
  useEffect(() => {
    if (!active) return;
    // Named regions when the caller gave them, `#root` otherwise. Resolved here rather than at
    // the call site so the cleanup un-sets exactly what it set, even if a ref has since changed.
    const isolated: HTMLElement[] = inertRefs
      ? inertRefs
          .map((r) => r.current)
          .filter((el): el is HTMLElement => el !== null)
      : [document.getElementById("root")].filter(
          (el): el is HTMLElement => el !== null,
        );
    // Only add `inert` where it was not already set. An element that was inert before this
    // drawer opened must stay inert after it closes — clearing it would hand interactivity to
    // something another surface had deliberately switched off.
    const added = isolated.filter((el) => !el.hasAttribute("inert"));
    for (const el of added) el.setAttribute("inert", "");
    return () => {
      for (const el of added) el.removeAttribute("inert");
    };
    // `inertRefs` IS a dependency here — this effect is about the isolation set, so a genuinely
    // different set must be re-applied. A caller that re-allocates an equivalent array re-runs
    // only this, which adds and removes the same attributes and is invisible.
  }, [active, inertRefs]);

  // Focus in, and back out to the trigger on close.
  useEffect(() => {
    if (!active) return;
    const trigger = triggerRef.current;
    initialFocusRef.current?.focus();
    return () => {
      // The restore waits a frame. On the Escape path a synchronous `focus()` sticks, but when
      // the drawer is dismissed by a TAP the browser is still settling focus from that pointer
      // sequence and finishes after this passive cleanup — landing on <body> and silently undoing
      // the restore. A frame later the event is done and the trigger keeps it.
      requestAnimationFrame(() => trigger?.focus());
    };
  }, [active, initialFocusRef, triggerRef]);

  // `containFocus` gates the CALL rather than living inside the hook: the menu's opt-out is a
  // statement about this drawer, while the hook is a primitive three other surfaces now share.
  useFocusContainment({
    active: active && containFocus,
    panelRef,
    // Registered by the drawer itself above, so the containment hook must not push a SECOND token
    // for the same surface — that would make the drawer permanently "below" itself. Note this is
    // independent of `containFocus`: the drawer is still an open SURFACE when its menu opts out
    // of containment, so its registration must not ride on that flag either.
    register: false,
    hasSurfaceAbove,
  });
}

/** Tab containment on its own, for a surface that already owns its focus-in, its Escape and its
 *  restore (#940 review 2).
 *
 *  The three modals the sidebar's ⋯ menu opens — Session brief, Hand off, Move to project — each
 *  do all three correctly and none of them contains Tab, so `aria-modal` was a promise they only
 *  half kept. (Two portal to `<body>`; Move to project renders in the sidebar row. That
 *  difference is invisible here, and making it invisible is the point — see
 *  `nestedSurfaceHasFocus`.) That was survivable while nothing else was modal; once the drawer beneath them
 *  became one, "focus wanders out of the child" and "the parent hauls it back" were two halves of
 *  the same broken interaction. `useModalDrawer` stands the parent down; this gives the child the
 *  containment that makes standing down the right thing to do.
 *
 *  Deliberately NOT the whole `useModalDrawer`: these surfaces already move focus in and restore
 *  it on unmount, and layering a second implementation over that is how two close paths end up
 *  fighting. This is the one piece they were missing.
 *
 *  Background isolation is NOT here either, and that is a scope call rather than an oversight:
 *  making `#root` inert behind these three would change what is reachable behind modals this
 *  change does not otherwise touch, and they were equally un-isolated before it. Noted for a
 *  follow-up.
 */
export function useFocusContainment({
  active,
  panelRef,
  register = true,
  hasSurfaceAbove,
}: {
  active: boolean;
  panelRef: RefObject<HTMLElement | null>;
  /** Push onto the modal stack while active. Default `true`: a child dialog calling this hook IS
   *  the surface. `useModalDrawer` passes `false` because it has already registered itself. */
  register?: boolean;
  /** Supplied by `useModalDrawer` so the drawer's own registration is the one consulted. */
  hasSurfaceAbove?: () => boolean;
}): void {
  const ownStack = useSurfaceStack(active && register);
  const above = hasSurfaceAbove ?? ownStack;
  // `inert` already stops the background taking focus in browsers that support it; this keeps the
  // cycle correct inside the panel either way.
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const host = panelRef.current;
      if (!host) return;
      // The topmost surface owns the keyboard. See `nestedSurfaceHasFocus` and `surfaceStack`.
      if (above()) return;
      if (nestedSurfaceHasFocus(host, document.activeElement)) return;
      const focusable = Array.from(
        host.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const activeEl = document.activeElement;
      const inside = activeEl instanceof Node && host.contains(activeEl);
      // WHERE IN THE CYCLE ARE WE? `-1` covers two different situations and both need the press
      // placed rather than left to the browser: focus is outside the panel entirely, or it is
      // inside on something that is not a tab stop.
      //
      // The second one is not hypothetical (#940 review 5). A dialog that focuses its own
      // `tabIndex={-1}` container on mount is inside the panel and absent from this list, so
      // comparing against `first`/`last` matched neither and the FIRST Shift+Tab walked straight
      // out of the dialog. Position, not identity.
      const at =
        activeEl instanceof HTMLElement ? focusable.indexOf(activeEl) : -1;
      const atStart = !inside || at <= 0;
      const atEnd = !inside || at === -1 || at === focusable.length - 1;
      if (e.shiftKey && atStart) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && atEnd) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [active, panelRef, above]);
}
