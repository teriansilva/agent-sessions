import {
  ArrowDown,
  ArrowRightToLine,
  ArrowUp,
  Copy,
  CornerDownLeft,
  MoreHorizontal,
  Paperclip,
  Square,
} from "lucide-react";
import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { KEYSEQ, type KeyName } from "../../lib/termKeys";
import styles from "./Compose.module.css";

interface KeyAction {
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

/** The compose key bar (#234): nav/control chips on a single row. When they don't all fit, the
 *  trailing ones collapse behind a "…" button that opens a small popover menu — no wrapping to
 *  a second row. Measurement-driven (ResizeObserver); when measurement is unavailable (jsdom)
 *  it defaults to all-inline so behaviour + tests are unchanged. */
export function KeyBar({
  sendInput,
  onCopy,
  onAttach,
}: {
  sendInput: (d: string) => void;
  onCopy: () => void;
  onAttach: () => void;
}) {
  const key = (name: KeyName) => sendInput(KEYSEQ[name]);
  const actions: KeyAction[] = [
    { id: "up", aria: "Up", title: "Up", icon: <ArrowUp size={16} />, run: () => key("up") },
    { id: "down", aria: "Down", title: "Down", icon: <ArrowDown size={16} />, run: () => key("down") },
    {
      id: "enter",
      aria: "Enter",
      title: "Enter",
      icon: <CornerDownLeft size={16} />,
      run: () => key("enter"),
    },
    { id: "esc", aria: "Escape", title: "Escape", text: "esc", run: () => key("esc") },
    {
      id: "tab",
      aria: "Tab",
      title: "Tab",
      icon: <ArrowRightToLine size={16} />,
      run: () => key("tab"),
    },
    {
      id: "interrupt",
      aria: "Interrupt (send Ctrl-C)",
      title: "Send Ctrl-C (interrupt)",
      icon: <Square size={14} fill="currentColor" />,
      run: () => key("ctrlc"),
    },
    {
      id: "attach",
      aria: "Attach file",
      title: "Attach an image or file",
      icon: <Paperclip size={16} />,
      run: onAttach,
    },
    { id: "copy", aria: "Copy", title: "Copy selection", icon: <Copy size={16} />, run: onCopy },
  ];

  const wrapRef = useRef<HTMLDivElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  // Natural per-button widths, cached from the widest render we've seen (the first, all-inline
  // one). We never re-measure the reduced set, so moving buttons into the menu can't loop.
  const widthsRef = useRef<number[]>([]);
  const [visible, setVisible] = useState(actions.length);
  const [menuOpen, setMenuOpen] = useState(false);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
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
    recompute();
    // No ResizeObserver (jsdom / very old browsers) → the initial recompute stands; we just
    // won't re-measure on resize. clientWidth is 0 there anyway, so everything stays inline.
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(recompute);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Close the overflow menu on outside click or Escape.
  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

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
      {overflow && menuOpen && (
        <div className={styles.keyMenu} role="menu">
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
                {a.text ? <span className={styles.txt}>{a.text}</span> : a.icon}
              </span>
              <span>{a.aria}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
