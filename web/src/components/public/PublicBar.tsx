import { ChevronDown, ChevronUp } from "lucide-react";
import { useCallback, useState, type ReactNode } from "react";
import { BrandMark } from "../brand/Brand";
import { BATTLELAB_SITE_URL } from "../../lib/links";
import styles from "./PublicBar.module.css";

/** The one header of every public page (#2068): a shared session (`/p/`), a shared reply (`/r/`)
 *  and a publicly shared artifact (`/artifacts/p/<id>/`).
 *
 *  - The mark is a link to battlelabos.com in a NEW tab, so a visitor never loses the share they
 *    are reading; `noopener noreferrer` gives that tab no handle on this one and no Referer (the
 *    page's address can carry a share token).
 *  - The bar collapses to a small tab (the mark and an expand control) floating over the page's
 *    top-right corner, so the content below gets the whole viewport. The choice lasts for the
 *    visit (`sessionStorage`); storage a browser refuses just means it isn't remembered.
 *  - `children` are the page's own facts (title, chips), shown only while the bar is open. */
export const COLLAPSE_KEY = "battlelab.publicBar.collapsed";

function readCollapsed(): boolean {
  try {
    return window.sessionStorage.getItem(COLLAPSE_KEY) === "1";
  } catch {
    return false;
  }
}

function writeCollapsed(collapsed: boolean): void {
  try {
    if (collapsed) window.sessionStorage.setItem(COLLAPSE_KEY, "1");
    else window.sessionStorage.removeItem(COLLAPSE_KEY);
  } catch {
    // Storage refused (private mode, a blocked origin): the bar still toggles, just not remembered.
  }
}

export function PublicBar({ children }: { children?: ReactNode }) {
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const toggle = useCallback(() => {
    setCollapsed((c) => {
      writeCollapsed(!c);
      return !c;
    });
  }, []);

  const brand = (
    <a
      className={styles.brand}
      href={BATTLELAB_SITE_URL}
      target="_blank"
      rel="noopener noreferrer"
      aria-label="BattleLab: battlelabos.com (opens in a new tab)"
      data-testid="public-bar-brand"
    >
      <BrandMark size={20} />
      {!collapsed && <span className={styles.name}>BattleLab</span>}
    </a>
  );

  if (collapsed)
    return (
      <header className={styles.tab} data-testid="public-bar" data-collapsed="true">
        {brand}
        <button
          type="button"
          className={styles.toggle}
          onClick={toggle}
          aria-expanded={false}
          aria-label="Show the header"
          title="Show the header"
          data-testid="public-bar-toggle"
        >
          <ChevronDown size={16} aria-hidden="true" />
        </button>
      </header>
    );

  return (
    <header className={styles.bar} data-testid="public-bar" data-collapsed="false">
      {brand}
      {children}
      <button
        type="button"
        className={styles.toggle}
        onClick={toggle}
        aria-expanded={true}
        aria-label="Collapse the header"
        title="Collapse the header"
        data-testid="public-bar-toggle"
      >
        <ChevronUp size={16} aria-hidden="true" />
      </button>
    </header>
  );
}
