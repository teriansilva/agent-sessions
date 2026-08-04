import { MoreHorizontal } from "lucide-react";
import { createPortal } from "react-dom";
import { type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import menu from "../files/filePanel.module.css";

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
  /** `trigger` is the element focus should return to when whatever this opens is closed. For an
   *  overflow item that is the persistent "…" button, NOT the menu item — the item unmounts with
   *  the menu, and a modal handed a detached node cannot restore focus at all. */
  run: (trigger?: HTMLElement | null) => void;
}

const GAP = 6; // matches .headActions gap

/** Pane-head actions with measurement-driven overflow (#783).
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
 *  The menu is portalled to <body> because `.terminal-pane` is `overflow: hidden` — the same
 *  reason KeyBar portals its own. KeyBar supplies the measurement and portal pattern only; the
 *  menu a11y (focus-in, arrow keys, Esc, focus return, roving `menuitem`) is implemented here. */
export function HeadActions({ actions, className, btnClassName, labelClassName }: {
  actions: HeadAction[];
  className: string;
  btnClassName: string;
  /** Wraps the visible text so the stylesheet can drop it on coarse pointers. */
  labelClassName: string;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const widths = useRef<number[]>([]);
  const [fit, setFit] = useState({ sig: "", n: actions.length });
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);

  // A stable identity for "which actions are these". `actions` is rebuilt every render, so keying
  // anything on the array itself re-runs on every commit and re-measures against a target that is
  // still moving — the head visibly thrashes.
  const sig = actions.map((a) => a.id).join(",");

  // Derived-state reconciliation during render (not an effect): when the action set changes, show
  // them all and re-measure from scratch. setState-during-render is React's documented way to
  // adjust state to a prop change, and it avoids the extra commit an effect would cost.
  if (fit.sig !== sig) {
    setFit({ sig, n: actions.length });
  }
  const visible = fit.sig === sig ? fit.n : actions.length;

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      // Recapture the natural widths whenever every chip is on the bar — which is exactly the
      // state a new action set starts in, so no explicit reset is needed (and a ref write during
      // render would not be allowed anyway).
      const kids = Array.from(el.querySelectorAll<HTMLElement>("[data-head-action]"));
      if (kids.length === actions.length) {
        widths.current = kids.map((k) => k.offsetWidth);
      }
      const bar = el.parentElement?.clientWidth ?? 0;
      if (!bar || widths.current.length !== actions.length) return;
      // Budget = the whole bar minus the irreducible left identity (LED + engine box) and the
      // bar's own padding. Per Terminal.module.css the meta run absorbs ALL shrink, so the
      // actions may legitimately take everything else — an earlier `bar * 0.66` guess collapsed
      // the head at 412px where all four chips fit comfortably, needlessly burying Recap and
      // Hand off behind the menu on every phone.
      const IDENTITY_W = 54; // LED + engine box
      const PAD = 20;
      const avail = bar - IDENTITY_W - PAD;
      const MORE_W = 34; // the "…" chip, reserved only when something will actually overflow
      const total = widths.current.reduce((a, b) => a + b, 0) + GAP * (actions.length - 1);
      if (total <= avail) {
        setFit((prev) => (prev.sig === sig && prev.n === actions.length ? prev : { sig, n: actions.length }));
        return;
      }
      let used = 0;
      let fitCount = 0;
      for (let i = 0; i < actions.length; i++) {
        const next = used + widths.current[i] + (i ? GAP : 0);
        if (next + GAP + MORE_W > avail) break;
        used = next;
        fitCount++;
      }
      const next = Math.max(1, fitCount);
      setFit((prev) => (prev.sig === sig && prev.n === next ? prev : { sig, n: next }));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    if (el.parentElement) ro.observe(el.parentElement);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);

  const inline = actions.slice(0, visible);
  const overflow = actions.slice(visible);

  const place = useCallback(() => {
    const r = moreRef.current?.getBoundingClientRect();
    if (r) setPos({ top: r.bottom + 4, right: Math.max(8, window.innerWidth - r.right) });
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
    window.addEventListener("scroll", place, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
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
  useEffect(() => {
    if (!open || !pos) return;
    menuRef.current?.querySelector<HTMLElement>("[role='menuitem']:not([disabled])")?.focus();
  }, [open, pos]);

  return (
    <div className={className} ref={wrapRef}>
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
      {overflow.length > 0 && (
        <>
          <button
            ref={moreRef}
            type="button"
            className={btnClassName}
            aria-haspopup="menu"
            aria-expanded={open}
            aria-label="More session actions"
            title="More session actions"
            onClick={() => setOpen((o) => !o)}
          >
            <MoreHorizontal size={13} aria-hidden="true" />
          </button>
          {open &&
            pos &&
            createPortal(
              <div
                ref={menuRef}
                className={menu.headMenu}
                role="menu"
                aria-label="More session actions"
                style={{ top: pos.top, right: pos.right }}
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
