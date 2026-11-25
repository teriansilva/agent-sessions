/** The top bar's working mark (#2103): what HQ is working on, one tap away on every route.
 *
 *  Its own control beside the HQ icon, not a badge on it: an LED and the number of tasks, with
 *  a "?" when one is parked on a question. Hidden when nothing runs, queues or waits. It opens
 *  the tasks strip; the HQ icon still opens HQ.
 *
 *  **Two forms, one strip.** Above 800px an anchored popover (portalled: the top bar is a
 *  `backdrop-filter` stacking context, #752), closed by an outside press, Escape or the mark.
 *  At ≤800px the shared bottom sheet (`ui/menu.module.css`) with a scrim and the whole
 *  `useModalDrawer` contract; it scrolls, and its head (Tasks, Stop all, Close) stays in reach.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useModalDrawer } from "../../pulse/useModalDrawer";
import menu from "../../ui/menu.module.css";
import { workingMark } from "./hqTasks";
import { useHqTasks } from "./hqTasksStore";
import { HqTasksStrip } from "./HqTasksStrip";
import s from "./HqTasks.module.css";

/** The shell's one phone line (docs/design.md §5). */
const SHEET_QUERY = "(max-width: 800px)";

export function HqTasksMark() {
  const { list } = useHqTasks();
  const mark = workingMark(list.counts);
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);
  const wrapRef = useRef<HTMLSpanElement | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const [sheet, setSheet] = useState(() => window.matchMedia?.(SHEET_QUERY).matches ?? false);
  useEffect(() => {
    const mq = window.matchMedia?.(SHEET_QUERY);
    if (!mq) return;
    const on = () => setSheet(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  const close = useCallback(() => setOpen(false), []);

  useModalDrawer({
    active: open && sheet,
    panelRef,
    initialFocusRef: closeRef,
    triggerRef: btnRef,
    onClose: close,
    insideRefs: [wrapRef],
  });

  // The anchored popover is not a modal: an outside press or Escape closes it, and focus goes
  // back to the mark.
  useEffect(() => {
    if (!open || sheet) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (wrapRef.current?.contains(t) || panelRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      btnRef.current?.focus();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, sheet]);

  const measure = useCallback(() => {
    const r = btnRef.current?.getBoundingClientRect();
    if (r) setAnchor({ top: r.bottom + 4, right: Math.max(8, window.innerWidth - r.right) });
  }, []);
  useEffect(() => {
    if (!open || sheet) return;
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [open, sheet, measure]);

  // Nothing runs, queues or waits: no mark. An open strip keeps it until it is closed, so the
  // control that opened the strip never vanishes under the Commander's hand.
  if (!mark && !open) return null;

  const strip = (
    <HqTasksStrip
      host="topbar"
      onNavigate={close}
      onClose={sheet ? close : undefined}
      closeRef={closeRef}
    />
  );
  const panel = !open
    ? null
    : sheet
      ? createPortal(
          <>
            <div className={menu.scrim} onClick={close} data-testid="hq-tasks-scrim" />
            <div className={menu.sheetWrap}>
              <div
                ref={panelRef}
                className={menu.sheet}
                role="dialog"
                aria-modal="true"
                aria-label="HQ tasks"
                data-testid="hq-tasks-sheet"
              >
                {strip}
              </div>
            </div>
          </>,
          document.body,
        )
      : createPortal(
          <div
            ref={panelRef}
            className={s.pop}
            role="dialog"
            aria-label="HQ tasks"
            style={anchor ? { top: anchor.top, right: anchor.right } : undefined}
            data-testid="hq-tasks-popover"
          >
            {strip}
          </div>,
          document.body,
        );

  return (
    <span className={s.markWrap} ref={wrapRef} data-topbar-keep="">
      <button
        ref={btnRef}
        type="button"
        className={`${s.mark} ${open ? s.markOn : ""}`}
        onClick={() => {
          if (!open) measure();
          setOpen((o) => !o);
        }}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={mark ? mark.label : "HQ tasks: nothing running"}
        title="HQ tasks"
        data-testid="hq-tasks-mark"
      >
        <span className={`hud-led ${mark ? "" : "idle"}`} aria-hidden="true" />
        <span aria-hidden="true">{mark ? mark.count : 0}</span>
        {mark?.question ? (
          <span className={s.markAsk} aria-hidden="true" data-testid="hq-tasks-mark-question">
            ?
          </span>
        ) : null}
      </button>
      {panel}
    </span>
  );
}
