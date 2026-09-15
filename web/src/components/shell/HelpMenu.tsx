/** The `?` in the shell (#987): the intro tour, the documentation and What's new behind one trigger.
 *
 *  Until #987 the `?` went straight into the tour, so an operator with a question had no route to
 *  the manual from inside the app. It is the shared `AnchoredMenu` — the mission `⋯` contract —
 *  with RowMenu's rows, focusing its first item on open (none of them is destructive).
 *
 *  The items only CALL what the shell already owns (`setTourOpen`, `whatsNew.open`); the menu holds
 *  no state of its own beyond being open. Every item closes the menu, and Documentation is a real
 *  link rather than a `window.open`, so the browser's own new-tab affordances (middle-click,
 *  long-press) keep working.
 */
import {
  BookOpen,
  Compass,
  ExternalLink,
  HelpCircle,
  Sparkles,
} from "lucide-react";

import { DOCS_HOME_URL } from "../../lib/links";
import { AnchoredMenu } from "../ui/AnchoredMenu";
import styles from "./HelpMenu.module.css";

const DOCS_HOST = new URL(DOCS_HOME_URL).host;

export function HelpMenu({
  align = "end",
  onTour,
  onWhatsNew,
  whatsNewLabel,
}: {
  /** `"end"` right-aligns the panel under the trigger (top bar); `"start"` left-aligns it (the
   *  drawer, where the `?` is the leftmost action and a right-aligned panel would leave it). */
  align?: "end" | "start";
  onTour: () => void;
  onWhatsNew: () => void;
  /** `whatsNewLabel()`; `null` when no release notes are bundled, which omits the item. */
  whatsNewLabel: string | null;
}) {
  return (
    <AnchoredMenu
      label="Help"
      trigger={<HelpCircle size={18} aria-hidden="true" />}
      triggerClassName="gear"
      triggerTestId="help-menu"
      menuTestId="help-menu-panel"
      focus="first-item"
      // The top bar's `?` must portal (see `AnchoredMenu`'s `portal`); the drawer's renders in
      // place, inside the drawer that already sits above the page.
      portal={align === "end"}
      classes={{
        wrap: styles.wrap,
        panel: `${styles.panel} ${align === "start" ? styles.start : ""}`,
        items: styles.items,
      }}
    >
      {(close) => (
        <>
          <button
            type="button"
            role="menuitem"
            className={styles.item}
            onClick={() => {
              close();
              onTour();
            }}
          >
            <span className={styles.icon} aria-hidden="true">
              <Compass size={15} />
            </span>
            Intro tour
          </button>
          <a
            role="menuitem"
            className={styles.item}
            href={DOCS_HOME_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Documentation (opens in a new tab)"
            onClick={close}
          >
            <span className={styles.icon} aria-hidden="true">
              <BookOpen size={15} />
            </span>
            <span className={styles.label}>
              Documentation
              <span className={styles.sub}>{DOCS_HOST}</span>
            </span>
            <span className={styles.trail} aria-hidden="true">
              <ExternalLink size={13} />
            </span>
          </a>
          {whatsNewLabel ? (
            <button
              type="button"
              role="menuitem"
              className={styles.item}
              onClick={() => {
                close();
                onWhatsNew();
              }}
            >
              <span className={styles.icon} aria-hidden="true">
                <Sparkles size={15} />
              </span>
              {whatsNewLabel}
            </button>
          ) : null}
        </>
      )}
    </AnchoredMenu>
  );
}
