/** HQ's work views (#2037). These are links over existing routes, not a second conversation or
 * mission owner. The shell and every HQ page use the same destinations. */
import { Link, useLocation } from "react-router-dom";
import { activeSubsection, HQ_VIEWS } from "../shell/sections";
import styles from "./HqPage.module.css";

export function HqViews() {
  const { pathname } = useLocation();
  const current = activeSubsection(pathname);
  return (
    <nav className={styles.views} aria-label="HQ views">
      {HQ_VIEWS.map(({ id, to, label }) => (
        <Link key={id} to={to!} className={styles.view} aria-current={current === id ? "page" : undefined}>
          {label}
        </Link>
      ))}
    </nav>
  );
}
