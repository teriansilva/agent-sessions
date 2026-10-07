import { MoreHorizontal } from "lucide-react";
import { createPortal } from "react-dom";
import { type ReactNode, type RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import menu from "../files/filePanel.module.css";
import { foldSelection } from "./headActionsFold";

export interface HeadAction {
  id: string;
  /** Visible chip text. */
  label: string;
  /** Accessible name. Kept SEPARATE from `label` because the shipped buttons already have
   *  descriptive aria-labels ("Open session brief", not "Recap") that tests and screen readers
   *  depend on — collapsing the two silently renamed them. */
  aria: string;
  title: string;
  icon: ReactNode;
  active?: boolean;
  disabled?: boolean;
  /** The overflow menu is this action's canonical home (#1329): a window's chrome bar is slim
   *  by default and only surfaces this action as a chip when the bar has room to spare. A
   *  session-mirrored pane action sets it — the session ⋯ menu already carries it under a stable
   *  name, so the chip is the exception, not the rule. */
  menuFirst?: boolean;
  /** `trigger` is the element focus should return to when whatever this opens is closed. For an
   *  overflow item that is the persistent "…" button, NOT the menu item — the item unmounts with
   *  the menu, and a modal handed a detached node cannot restore focus at all. */
  run: (trigger?: HTMLElement | null) => void;
}

/** Whether two folds kept the same actions — the state identity check that keeps a settled fold
 *  from re-rendering on every ResizeObserver tick. */
function sameSelection(a: boolean[], b: boolean[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

const GAP = 6; // matches .headActions gap



/** Pane-head actions with measurement-driven overflow (#783).
 *
 *  **At the shell's ≤800px breakpoint there is no measuring (#948 P6).** `collapsed` puts every
 *  action into the one "Actions for this session" menu, capped to the viewport and scrollable. The
 *  measured fold described below applies to wider panes only.
 *
 *  The header carries a *measured* contract (`Terminal.module.css`): three labelled buttons occupy
 *  ~240px and "button labels are never hidden … the labelled buttons still fit a 320px pane". A
 *  fourth (`Files`) breaks that at a 320px pane and is marginal at the 360px `PANE_MIN`. Shrinking
 *  to icon-only is what that same comment rejects on touch-target grounds.
 *
 *  So this reuses the idiom already shipped in `KeyBar`: trailing actions fold into one `…` chip
 *  whose menu still carries **full labels**. Labels are not hidden — they move. `Files` leads (the
 *  new primary affordance) and `Repaint` stays out of the menu: burying the recovery control when
 *  the screen is blank is the wrong trade.
 *
 *  **A `menuFirst` action folds before the trailing ones (#1329).** The overflow menu is its
 *  canonical home — a window's chrome bar is slim by default and only surfaces it as a chip when the
 *  bar has room to spare. A session-mirrored pane action (Recap, Hand off, mission) sets it, because
 *  the session ⋯ menu already carries it under a stable name; folding it first is what stops the bar
 *  from showing a chip AND its menu twin. `foldSelection` returns a per-action boolean, so a `menuFirst`
 *  action folding from the middle leaves no hole — the flex row packs what is left.
 *
 *  The menu is portalled to <body> because `.terminal-pane` is `overflow: hidden` — the same
 *  reason KeyBar portals its own. KeyBar supplies the measurement and portal pattern only; the
 *  menu a11y (focus-in, arrow keys, Esc, focus return, roving `menuitem`) is implemented here.
 *
 *  **A map window folds into ITS chrome's ⋯ instead (#1109).** `foldInto: "external"` renders no
 *  "…" trigger and no portal menu of its own — the window bar already carries exactly one ⋯, the
 *  one that opens the merged session+pane menu — and publishes whatever overflowed through
 *  `overflowRef` after every commit, so the chrome reads the CURRENT fold at menu-open time
 *  without a single extra render. The chips that fit stay chips; the rest are reachable only
 *  through that one menu, which is the point: a window never shows two menus. */
export function HeadActions({ actions, className, btnClassName, labelClassName, collapsed = false, reservePx, foldInto = "self", overflowRef, allRef, barRef }: {
  actions: HeadAction[];
  className: string;
  btnClassName: string;
  /** Wraps the visible text so the stylesheet can drop it on coarse pointers. */
  labelClassName: string;
  /** ONE MENU ON A SMALL SCREEN (#948 P6). At the shell's ≤800px breakpoint the header renders a
   *  single labelled "Actions" trigger and every action lives in the menu, with its full label.
   *  This deliberately replaces #744/#859's "every action one tap away on touch" on phones only —
   *  the operator asked for the header to become a context menu there. Wider panes, touch or not,
   *  keep the measured fold below. */
  collapsed?: boolean;
  /** The width, in px, the bar must still hold BESIDES the chips — identity run, title, fixed
   *  buttons. The pane's own bar is modelled by the built-in default (LED + engine box + the
   *  bar's padding; the meta run is the designated shrink absorber there). A window's chrome bar
   *  passes a MEASURED reserve instead (#1109): its facts run is `flex: none`, so its width is a
   *  fact, and the title + window buttons + grip are constants the chrome owns. Folding late
   *  would clip the title; folding a little early is the safe direction. */
  reservePx?: number;
  /** Where the overflow goes. `"self"` (default): this component renders its own "…" trigger +
   *  portal menu, exactly as shipped. `"external"` (#1109): a host that carries the ONE merged
   *  menu itself — no trigger, no menu here, and the overflow published via `overflowRef`. */
  foldInto?: "self" | "external";
  /** With `foldInto: "external"`: the host's ref to read the overflowed actions from, updated
   *  after every commit so a menu built at open time is never stale. */
  overflowRef?: { current: HeadAction[] };
  /** With `foldInto: "external"`: the host's ref to read the FULL action list from (#1329),
   *  published after every commit beside the overflow. A host whose merged menu must omit only the
   *  twins of actions this pane really offers reads it — `overflowRef` cannot say whether an action
   *  the fold kept on the bar is offered at all. */
  allRef?: { current: HeadAction[] };
  /** The BAR the fold budgets against. Default is this wrapper's parent — true in the pane,
   *  where the wrapper sits directly under `.panelHead`; a window's chips sit in a dedicated
   *  slot span instead, so the host passes its bar (`[data-window-head]`) explicitly (#1109).
   *  The observer watches the same element: observing the slot would measure the fold's own
   *  output, an unstable feedback loop. */
  barRef?: RefObject<HTMLElement | null>;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const widths = useRef<number[]>([]);
  // `w` is the bar width the committed fit was measured at — the measurement-complete invariant
  // the #744 ladder spec waits on (`data-fit-width`), since "the '…' trigger exists" cannot tell a
  // settled narrower rung from the previous wider one (#909). `sel` is WHICH actions that fit kept —
  // a boolean per action, because a `menuFirst` action folds from the middle (#1329). Absent until the
  // first measurement.
  const [fit, setFit] = useState<{ sig: string; sel: boolean[]; w?: number }>({
    sig: "",
    sel: [],
  });
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; right: number; maxHeight: number } | null>(null);

  // A stable identity for "which actions are these". `actions` is rebuilt every render, so keying
  // anything on the array itself re-runs on every commit and re-measures against a target that is
  // still moving — the head visibly thrashes.
  const sig = actions.map((a) => a.id).join(",");

  // Derived-state reconciliation during render (not an effect): when the action set changes, show
  // them all and re-measure from scratch. setState-during-render is React's documented way to
  // adjust state to a prop change, and it avoids the extra commit an effect would cost.
  if (fit.sig !== sig) {
    setFit({ sig, sel: actions.map(() => true) });
  }
  const selected = collapsed
    ? actions.map(() => false)
    : fit.sig === sig
      ? fit.sel
      : actions.map(() => true);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    // Collapsed, there are no chips to measure — everything is in the menu.
    if (collapsed || !el || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      // Recapture the natural widths whenever every chip is on the bar — which is exactly the
      // state a new action set starts in, so no explicit reset is needed (and a ref write during
      // render would not be allowed anyway).
      const kids = Array.from(el.querySelectorAll<HTMLElement>("[data-head-action]"));
      if (kids.length === actions.length) {
        widths.current = kids.map((k) => k.offsetWidth);
      }
      const bar = barRef?.current?.clientWidth ?? el.parentElement?.clientWidth ?? 0;
      if (!bar || widths.current.length !== actions.length) return;
      // Budget = the whole bar minus the irreducible left identity (LED + engine box) and the
      // bar's own padding. Per Terminal.module.css the meta run absorbs ALL shrink, so the
      // actions may legitimately take everything else — an earlier `bar * 0.66` guess collapsed
      // the head at 412px where all four chips fit comfortably, needlessly burying Recap and
      // Hand off behind the menu on every phone. A window's chrome bar passes a measured
      // `reservePx` instead (#1109): its facts run is fixed-width, its title and window buttons
      // are constants, and the fold must budget for them or it would clip the title.
      const IDENTITY_W = reservePx ?? 54; // LED + engine box (the pane model)
      const PAD = 20;
      const avail = bar - IDENTITY_W - PAD;
      // The "…" chip is reserved only when something will actually overflow — and only when THIS
      // component renders the trigger at all. `foldInto: "external"` folds into a menu the host
      // owns, mounted outside this slot's flow, so nothing here is reserved for it.
      const MORE_W = foldInto === "external" ? 0 : 34;
      const next = foldSelection(
        widths.current,
        avail,
        GAP,
        MORE_W,
        actions.map((a) => Boolean(a.menuFirst)),
      );
      setFit((prev) =>
        prev.sig === sig && prev.w === bar && sameSelection(prev.sel, next)
          ? prev
          : { sig, sel: next, w: bar },
      );
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    const watched = barRef?.current ?? el.parentElement;
    if (watched) ro.observe(watched);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig, collapsed, reservePx]);

  const inline = actions.filter((_, i) => selected[i]);
  const overflow = actions.filter((_, i) => !selected[i]);

  // The host's fold is published after EVERY commit, unconditionally: the chrome builds its menu
  // from this ref at open time, so it must name the actions that are actually folded right now.
  // A ref write never re-renders, so this cannot loop — it is what makes the merged menu read
  // current state (disabled flips, a Repaint that just became possible) with no render coupling.
  useEffect(() => {
    if (overflowRef) overflowRef.current = overflow;
    // The FULL list too (#1329): a pane that offers NO action (a chat/api runtime, or the
    // one-frame gap before the first commit) must publish an EMPTY list, not leave a stale one,
    // so the host omits nothing and the session entries survive.
    if (allRef) allRef.current = actions;
    // Clear both on unmount (#1329): RuntimeGate mounts THIS Terminal while the engine roster
    // is unknown, then swaps in ChatPane/StructuredPane when a chat/api entry arrives. The host
    // keeps the same refs, so without this cleanup its ⋯ would still omit session actions the
    // (now unmounted) pane offered, and run stale callbacks from the torn-down terminal.
    return () => {
      if (overflowRef) overflowRef.current = [];
      if (allRef) allRef.current = [];
    };
  });

  const place = useCallback(() => {
    const r = moreRef.current?.getBoundingClientRect();
    if (!r) return;
    const top = r.bottom + 4;
    // On a phone EVERY action lives in this menu (#948 P6), so on a short viewport (a landscape
    // phone) it can be taller than the space under the trigger. Cap it to that space and let it
    // scroll, so the last action stays reachable (#958 review). Re-placed on resize and scroll.
    const maxHeight = Math.max(88, window.innerHeight - top - 8);
    setPos({ top, right: Math.max(8, window.innerWidth - r.right), maxHeight });
  }, []);

  useEffect(() => {
    if (!open) return;
    place();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
        moreRef.current?.focus();
        return;
      }
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      e.preventDefault();
      const items = Array.from(
        menuRef.current?.querySelectorAll<HTMLElement>("[role='menuitem']:not([disabled])") ?? [],
      );
      if (!items.length) return;
      const i = items.indexOf(document.activeElement as HTMLElement);
      // `i === -1` (focus still on the trigger) walks to the first item on ArrowDown and the last
      // on ArrowUp, rather than looping on an unreachable index.
      const next = e.key === "ArrowDown" ? (i + 1) % items.length : (i <= 0 ? items.length : i) - 1;
      items[next]?.focus();
    };
    const onDown = (e: PointerEvent) => {
      if (menuRef.current?.contains(e.target as Node) || moreRef.current?.contains(e.target as Node)) return;
      setOpen(false);
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("resize", place);
    // A scroll INSIDE the menu (the list scrolling under its own height cap, #958) must not re-place
    // it: that is the menu moving its content, not the trigger moving on the page.
    const onScroll = (e: Event) => {
      if (menuRef.current && e.target instanceof Node && menuRef.current.contains(e.target)) return;
      place();
    };
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open, place]);

  // A resize can make every action fit, which unmounts the menu (and its "…" trigger) underneath
  // whatever had focus. Close during render — the documented way to adjust state to a prop
  // change — and restore focus in an effect that touches no state.
  if (open && overflow.length === 0) setOpen(false);

  useEffect(() => {
    if (open) return;
    if (document.activeElement !== document.body) return;
    const fallback = wrapRef.current?.querySelector<HTMLElement>("[data-head-action]");
    (moreRef.current && document.contains(moreRef.current) ? moreRef.current : fallback)?.focus();
  }, [open]);

  // Focus the first ENABLED item, once the menu is really in the DOM. It renders on `open && pos`
  // and `pos` is set by the effect above, so doing this there ran against a null ref and did
  // nothing at all — the a11y contract only looked implemented. Repaint is disabled while the
  // socket is down and can be first in the overflow, so skipping disabled items matters here.
  // ONCE PER OPENING (#958 review 4810). `pos` changes on every resize and page scroll, and this used
  // to re-run on each one — so ArrowDown to an offscreen item scrolled it into view, the scroll re-placed
  // the menu, and focus jumped back to the first item. The ref is cleared when the menu closes.
  const focusedOnOpen = useRef(false);
  useEffect(() => {
    if (!open) {
      focusedOnOpen.current = false;
      return;
    }
    if (!pos || focusedOnOpen.current) return;
    focusedOnOpen.current = true;
    menuRef.current?.querySelector<HTMLElement>("[role='menuitem']:not([disabled])")?.focus();
  }, [open, pos]);

  return (
    <div
      className={className}
      ref={wrapRef}
      data-fit-width={fit.sig === sig && fit.w !== undefined ? fit.w : "unmeasured"}
    >
      {inline.map((a) => (
        <button
          key={a.id}
          type="button"
          data-head-action={a.id}
          className={btnClassName}
          onClick={(e) => a.run(e.currentTarget)}
          disabled={a.disabled}
          title={a.title}
          aria-label={a.aria}
          aria-pressed={a.active}
          style={a.active ? { borderColor: "var(--accent)", color: "var(--accent)" } : undefined}
        >
          {a.icon}
          <span className={labelClassName}>{a.label}</span>
        </button>
      ))}
      {/* The fold trigger lives HERE only in the self-hosted case. `foldInto: "external"` (#1109)
          leaves the overflow to the host's single ⋯ menu — a window must never render a second
          "…" chip beside its chrome's own. */}
      {overflow.length > 0 && foldInto === "self" && (
        <>
          <button
            ref={moreRef}
            type="button"
            className={btnClassName}
            aria-haspopup="menu"
            aria-expanded={open}
            aria-label={collapsed ? "Actions for this session" : "More session actions"}
            title={collapsed ? "Actions for this session" : "More session actions"}
            onClick={() => setOpen((o) => !o)}
            data-testid={collapsed ? "head-actions-menu" : undefined}
          >
            {collapsed ? <span>Actions</span> : null}
            <MoreHorizontal size={13} aria-hidden="true" />
          </button>
          {open &&
            pos &&
            createPortal(
              <div
                ref={menuRef}
                className={menu.headMenu}
                role="menu"
                aria-label={collapsed ? "Actions for this session" : "More session actions"}
                style={{ top: pos.top, right: pos.right, maxHeight: pos.maxHeight }}
              >
                {overflow.map((a) => (
                  <button
                    key={a.id}
                    type="button"
                    role="menuitem"
                    className={menu.headMenuItem}
                    disabled={a.disabled}
                    title={a.title}
                    aria-label={a.aria}
                    onClick={() => {
                      setOpen(false);
                      // Return focus to the "…" trigger, which survives the menu closing.
                      a.run(moreRef.current);
                    }}
                  >
                    {a.icon}
                    {a.label}
                  </button>
                ))}
              </div>,
              document.body,
            )}
        </>
      )}
    </div>
  );
}
