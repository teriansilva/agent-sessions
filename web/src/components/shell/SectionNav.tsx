/** The section nav: the top bar's row (#1058).
 *
 *  The list itself is `sections.ts`. The phone drawer used to repeat it as a labelled column; that
 *  copy is gone (#1069 follow-up) — it duplicated the bar directly above it and took a third of the
 *  drawer. At icon-only widths each entry is still named by its own text and its `title`.
 *
 *  **The label is always in the DOM.** At ≤640px the bar goes icon-only, and it does that by
 *  clipping the label visually (`App.css`), never by `display: none` — a link whose text is removed
 *  has no accessible name, and a row of unnamed icons is exactly the surface a screen-reader
 *  operator cannot use. Same text, same element, different paint. That is also why there is no
 *  `aria-label` here: a second copy of the name is a second copy to drift.
 *
 *  **A section with children is a SPLIT control in the bar (#1069).** The label stays a plain link
 *  to the section's own destination, and a chevron beside it opens the shared `AnchoredMenu` with
 *  every child. The menu is portalled: the desktop `.hud-topbar` is a `backdrop-filter` stacking
 *  context that the pane paints over (#752, #987). The same control serves a phone.
 *
 *  On a child's route the parent carries `aria-current="true"` rather than `"page"`: its link goes
 *  somewhere else (the last session), so claiming it IS the page would be false. The child's own
 *  menu entry is the one that says `"page"`.
 */
import { ChevronDown } from "lucide-react";
import { Link } from "react-router-dom";

import { AnchoredMenu } from "../ui/AnchoredMenu";
import {
  SECTIONS,
  type Section,
  type SectionId,
  type SubsectionId,
} from "./sections";
import styles from "./SectionNav.module.css";

export function SectionNav({
  active,
  activeSub = null,
  sessionsPath,
  onNavigate,
}: {
  /** Which section the current route belongs to, or `null` on a route that is none of them
   *  (Settings). Decided by the shell — see `activeSection`. */
  active: SectionId | null;
  /** Which sub-menu entry the route is, when it is one — see `activeSubsection`. */
  activeSub?: SubsectionId | null;
  /** Where Sessions points: the last session route seen, so leaving and coming back lands on the
   *  session you were in rather than the new-session landing. */
  sessionsPath: string;
  /** Closes the mobile drawer. A same-route tap does not change `location.pathname`, so the
   *  shell's route effect never fires and the drawer would stay open over the page (#283). */
  onNavigate: () => void;
}) {
  /** `"page"` when the route IS this link's target; `"true"` when it is one of the section's
   *  children that this link does not point at. */
  const parentCurrent = (s: Section) => {
    if (active !== s.id) return undefined;
    if (!s.children) return "page" as const;
    return activeSub === s.children[0].id
      ? ("page" as const)
      : ("true" as const);
  };

  const link = (s: Section) => (
    <Link
      key={s.id}
      to={s.to ?? sessionsPath}
      aria-current={parentCurrent(s)}
      onClick={onNavigate}
      // For the eye at icon-only widths; the link's own text is its accessible name.
      title={s.beta ? `${s.label} (beta)` : s.label}
      data-section={s.id}
    >
      <s.Icon size={16} aria-hidden="true" />
      <span className="section-nav-label">{s.label}</span>
      {/* BETA (#1085): for the eye only. The link's name stays "Missions" — the tag is not part
          of the destination, and every spec and screen reader that names it keeps working. The
          `title` says it in words. */}
      {s.beta ? (
        <span className="section-nav-beta" aria-hidden="true">
          Beta
        </span>
      ) : null}
    </Link>
  );

  return (
    <nav
      className="section-nav"
      aria-label="Main sections"
      data-testid="section-nav"
    >
      {SECTIONS.map((s) => {
        if (!s.children) return link(s);
        return (
          <span
            key={s.id}
            className={`section-nav-split${active === s.id ? " is-active" : ""}`}
          >
            {link(s)}
            <AnchoredMenu
              label={`${s.label} menu`}
              trigger={<ChevronDown size={14} aria-hidden="true" />}
              triggerClassName="section-nav-chevron"
              triggerTestId={`section-menu-${s.id}`}
              menuTestId={`section-menu-${s.id}-panel`}
              focus="first-item"
              portal
              classes={{
                wrap: styles.wrap,
                panel: styles.panel,
                items: styles.items,
              }}
            >
              {(close) =>
                s.children!.map((c) => (
                  <Link
                    key={c.id}
                    role="menuitem"
                    className={styles.item}
                    to={c.to ?? sessionsPath}
                    aria-current={
                      active === s.id && activeSub === c.id ? "page" : undefined
                    }
                    onClick={() => {
                      close();
                      onNavigate();
                    }}
                    data-subsection={c.id}
                  >
                    <span className={styles.icon} aria-hidden="true">
                      <c.Icon size={15} />
                    </span>
                    {c.label}
                  </Link>
                ))
              }
            </AnchoredMenu>
          </span>
        );
      })}
    </nav>
  );
}
