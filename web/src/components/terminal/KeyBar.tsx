import { MoreHorizontal } from "lucide-react";
import { createPortal } from "react-dom";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import styles from "./Compose.module.css";

export interface KeyAction {
  id: string;
  /** Stable aria-label (must not change — tests + a11y depend on these). */
  aria: string;
  title: string;
  /** Icon for the inline button; `text` is used instead when set (the "esc" chip). */
  icon?: ReactNode;
  text?: string;
  run: () => void;
}

const GAP = 4; // matches .keys gap

/** The compose key bar (#234/#487): the caller-provided action chips on a single row. When they
 *  don't all fit, the trailing ones collapse behind a "…" button that opens a small popover menu —
 *  no wrapping to a second row, and it is the ONLY menu in the bar. Measurement-driven
 *  (ResizeObserver); when measurement is unavailable (jsdom) it defaults to all-inline so behaviour
 *  + tests are unchanged. The action list (and its order) is owned by the parent (Compose). */
export function KeyBar({ actions }: { actions: KeyAction[] }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  // Natural per-button widths, cached from the widest render we've seen. Reset whenever the action
  // set changes so a new set (e.g. the open→collapsed close chip) is re-measured from scratch.
  const widthsRef = useRef<number[]>([]);
  const [visible, setVisible] = useState(actions.length);
  const [menuOpen, setMenuOpen] = useState(false);
  // The overflow popover is portalled to <body> with position:fixed, measured from the "…" trigger
  // (#500): the compose bar lives inside `.compose` (overflow-y:auto) and `.terminal-pane`
  // (overflow:hidden), so an in-tree popover gets clipped/offscreen on narrow widths. Opens upward.
  const [menuPos, setMenuPos] = useState<{
    bottom: number;
    left: number;
  } | null>(null);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    setVisible(actions.length); // default to all; recompute trims below if we can measure
    widthsRef.current = [];
    const recompute = () => {
      const avail = el.clientWidth;
      if (!avail) return; // unmeasured (jsdom / display:none) → keep all inline
      const btns = Array.from(el.querySelectorAll<HTMLElement>("[data-key]"));
      if (btns.length >= widthsRef.current.length) {
        widthsRef.current = btns.map((b) => b.offsetWidth + GAP);
      }
      const w = widthsRef.current;
      if (!w.length) return;
      const total = w.reduce((a, b) => a + b, 0);
      if (total <= avail) {
        setVisible(w.length);
        return;
      }
      const moreW = (moreRef.current?.offsetWidth ?? 34) + GAP;
      let used = moreW;
      let fit = 0;
      for (const bw of w) {
        if (used + bw <= avail) {
          used += bw;
          fit++;
        } else break;
      }
      setVisible(Math.max(1, fit));
    };
    // Measure AFTER paint, not synchronously: when the action set has just grown (e.g. the collapse
    // chip appearing on open), a synchronous measure reads the pre-render partial DOM (only the
    // currently-shown chips carry [data-key]) and wrongly trims a chip into the "…" even on a wide
    // desktop. RO + a RAF fallback both fire post-paint, once the freshly-shown full set is in DOM.
    let raf = 0;
    if (typeof requestAnimationFrame !== "undefined")
      raf = requestAnimationFrame(recompute);
    if (typeof ResizeObserver === "undefined") {
      return () => {
        if (raf) cancelAnimationFrame(raf);
      };
    }
    const ro = new ResizeObserver(recompute);
    ro.observe(el);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [actions.length]);

  // Place the portalled popover just ABOVE the "…" trigger, left-aligned (the bar sits at the
  // viewport bottom). `bottom` measured from the viewport bottom up to the trigger's top, so it
  // grows upward regardless of height; `left` clamped so it can't run off the left edge.
  const positionMenu = useCallback(() => {
    const el = moreRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    // Clamp left so the popover never runs off the right edge on narrow widths. Use the menu's real
    // width once rendered, else a generous estimate (it is also capped by max-width in CSS).
    const menuW = menuRef.current?.offsetWidth ?? 220;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - menuW - 8));
    setMenuPos({ bottom: Math.max(8, window.innerHeight - r.top + 6), left });
  }, []);

  // While the menu is open: position it, then dismiss on outside-click (the menu is portalled, so
  // check both the trigger and the menu), Escape, and reposition on resize/scroll.
  useEffect(() => {
    if (!menuOpen) return;
    positionMenu();
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (moreRef.current?.contains(t) || menuRef.current?.contains(t)) return;
      setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", positionMenu);
    window.addEventListener("scroll", positionMenu, true);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", positionMenu);
      window.removeEventListener("scroll", positionMenu, true);
    };
  }, [menuOpen, positionMenu]);

  const overflow = visible < actions.length;
  const shown = actions.slice(0, visible);
  const hidden = actions.slice(visible);

  return (
    <div className={styles.keys} ref={wrapRef}>
      {shown.map((a) => (
        <button
          key={a.id}
          data-key
          type="button"
          aria-label={a.aria}
          title={a.title}
          className={a.text ? styles.txt : undefined}
          onClick={a.run}
        >
          {a.text ?? a.icon}
        </button>
      ))}
      {overflow && (
        <button
          ref={moreRef}
          type="button"
          aria-label="More keys"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          title="More keys"
          className={styles.moreKey}
          onClick={() => setMenuOpen((o) => !o)}
        >
          <MoreHorizontal size={16} />
        </button>
      )}
      {overflow &&
        menuOpen &&
        menuPos &&
        createPortal(
          <div
            ref={menuRef}
            className={styles.keyMenu}
            role="menu"
            style={{ bottom: menuPos.bottom, left: menuPos.left }}
          >
            {hidden.map((a) => (
              <button
                key={a.id}
                type="button"
                role="menuitem"
                aria-label={a.aria}
                title={a.title}
                onClick={() => {
                  a.run();
                  setMenuOpen(false);
                }}
              >
                <span className={styles.keyMenuIcon}>
                  {a.text ? (
                    <span className={styles.txt}>{a.text}</span>
                  ) : (
                    a.icon
                  )}
                </span>
                <span>{a.aria}</span>
              </button>
            ))}
          </div>,
          document.body,
        )}
    </div>
  );
}
