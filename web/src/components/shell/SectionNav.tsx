/** The section nav, in two paints (#1058): the top bar's row and the drawer's labelled column.
 *
 *  The list itself is `sections.ts` — one array, two renderings, so a phone's drawer can never name
 *  four of the five icons in the bar above it.
 *
 *  **The label is always in the DOM.** At ≤640px the bar goes icon-only, and it does that by
 *  clipping the label visually (`App.css`), never by `display: none` — a link whose text is removed
 *  has no accessible name, and a row of five unnamed icons is exactly the surface a screen-reader
 *  operator cannot use. Same text, same element, different paint. That is also why there is no
 *  `aria-label` here: a second copy of the name is a second copy to drift.
 */
import { Link } from "react-router-dom";

import { SECTIONS, type SectionId } from "./sections";

export function SectionNav({
  active,
  sessionsPath,
  onNavigate,
  variant = "bar",
}: {
  /** Which section the current route belongs to, or `null` on a route that is none of them
   *  (Settings). Decided by the shell — see `activeSection`. */
  active: SectionId | null;
  /** Where Sessions points: the last session route seen, so leaving and coming back lands on the
   *  session you were in rather than the new-session landing. */
  sessionsPath: string;
  /** Closes the mobile drawer. A same-route tap does not change `location.pathname`, so the
   *  shell's route effect never fires and the drawer would stay open over the page (#283). */
  onNavigate: () => void;
  /** `"bar"` is the top bar's row; `"drawer"` is the labelled list inside the mobile drawer. */
  variant?: "bar" | "drawer";
}) {
  const bar = variant === "bar";
  return (
    <nav
      className={bar ? "section-nav" : "section-nav-drawer"}
      aria-label={bar ? "Main sections" : "Sections"}
      data-testid={bar ? "section-nav" : "section-nav-drawer"}
    >
      {SECTIONS.map(({ id, label, Icon, to }) => (
        <Link
          key={id}
          to={to ?? sessionsPath}
          aria-current={active === id ? "page" : undefined}
          onClick={onNavigate}
          // For the eye at icon-only widths; the link's own text is its accessible name.
          title={label}
          data-section={id}
        >
          <Icon size={16} aria-hidden="true" />
          <span className="section-nav-label">{label}</span>
        </Link>
      ))}
    </nav>
  );
}
