/** The operator tile in the corner (#1058) — who is signed in, and everything you do about the app.
 *
 *  **It is also where Help and Settings live (#1085).** The top bar used to carry a `?` menu and a
 *  ⚙ link beside this tile, and the drawer repeated both in its own action row — three places for
 *  the same five things. The operator asked for them here: this tile stays in the bar at every
 *  width (`data-topbar-keep`), so one menu reaches Settings, Security, the tour, the docs and
 *  What's new on a phone and on a desktop alike.
 *
 *  The corner used to be four anonymous icons, two of which were destinations (#1058 moved those
 *  into the section nav). What was left had no answer to "which account is this tab?" — on a tool
 *  that launches permission-bypassed agents on a real host, that is worth one tile.
 *
 *  **A square, not a circle.** `docs/design.md` §1: no rounded corners anywhere, only LEDs and
 *  status dots stay circular. A round avatar would be the single round corner in the app.
 *
 *  **It reuses `AnchoredMenu`, and `portal` is not optional here.** A trigger inside `.hud-topbar`
 *  sits in a `backdrop-filter` stacking context with no `z-index`, which the terminal pane paints
 *  over: a non-portalled panel draws, takes focus, and hands the click to the pane underneath
 *  (#752's bell, #987's Help menu). Portalling removes the stacking dependency rather than trying to
 *  out-number it. `focus="first-item"` because none of the items is destructive in the
 *  two-tap-confirmation sense — Sign out ends a session, it does not delete anything.
 *
 *  **THE MODE DECIDES, THE NAME ONLY LABELS.** `auth_mode` has been on `/api/config` since #13/#32
 *  and `username` since #1058, so the mode is the field that is always there and the one that
 *  answers the question that matters: is there a login at all? `"none"` is a LOCAL tile with no
 *  Sign out — matching what `SecurityPanel` already does with its own Sign out button — and a
 *  Sign out offered where there is no session would be a control the app cannot honour.
 *
 *  Reading `username` for that instead had two bugs in one line. A server too old to send it, or a
 *  mock that omits it, rendered NO TILE on an install that plainly has a login; and a
 *  login-protected install would have had to depend on a second field agreeing with the first. So
 *  a missing name under a real login is a nameless tile, never a LOCAL one: it understates what it
 *  knows instead of misdescribing the install's security.
 *
 *  `authMode === undefined` is the one "we do not know yet" case, and it renders NOTHING. A tile
 *  that says LOCAL for a frame and then flips to a username is worse than one that arrives late.
 */
import {
  BookOpen,
  Compass,
  ExternalLink,
  LogOut,
  Settings as SettingsIcon,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { Link, useLocation } from "react-router-dom";

import { api } from "../../lib/api";
import { DOCS_HOME_URL } from "../../lib/links";
import { settingsPath } from "../../routes/settingsTabs";
import { AnchoredMenu } from "../ui/AnchoredMenu";
import help from "./HelpMenu.module.css";
import styles from "./OperatorMenu.module.css";
import { operatorInitials } from "./operatorInitials";

const DOCS_HOST = new URL(DOCS_HOME_URL).host;

export function OperatorMenu({
  username,
  authMode,
  align = "end",
  onNavigate,
  onTour,
  onWhatsNew,
  whatsNewLabel = null,
}: {
  /** The operator's login name. `null` on a no-login install, `undefined` before the config
   *  arrives or on a server too old to send it — neither decides whether there IS a login. */
  username: string | null | undefined;
  /** `"single-user"` | `"none"`, from `/api/config`. `undefined` means the config has not
   *  arrived; it is the ONE signal that suppresses the tile. */
  authMode: string | undefined;
  /** `"end"` right-aligns the panel under the trigger (top bar); `"start"` left-aligns it. */
  align?: "end" | "start";
  /** Closes the mobile drawer on a same-route tap (#283). */
  onNavigate: () => void;
  /** Opens the intro tour. Absent ⇒ no tour item. The shell owns the tour; the menu only calls. */
  onTour?: () => void;
  /** Opens What's new. Shown only with `whatsNewLabel` (no bundled release notes ⇒ no item). */
  onWhatsNew?: () => void;
  /** `whatsNewLabel()` — e.g. "What's new in 0.20"; `null` when nothing is bundled. */
  whatsNewLabel?: string | null;
}) {
  // Settings returns to wherever it was opened from, as the old top-bar ⚙ did.
  const location = useLocation();
  // No config yet: render nothing rather than a placeholder identity.
  if (authMode === undefined) return null;

  const signedIn = authMode !== "none";
  // A login with no name is a NAMELESS tile, not a LOCAL one — see the module note.
  const label = signedIn ? (username ?? "Operator") : "Local";
  const initials = signedIn ? operatorInitials(username ?? "Operator") : "··";

  return (
    <AnchoredMenu
      label={
        signedIn
          ? username
            ? `Operator ${username}`
            : "Operator"
          : "Operator — no login"
      }
      trigger={
        <>
          <span className={styles.square} aria-hidden="true">
            {initials}
          </span>
          {/* Clipped, not removed, at icon-only widths — see App.css. The trigger's accessible
              name comes from `AnchoredMenu`'s own label, so this text is for the eye. */}
          <span className={styles.name} aria-hidden="true">
            {label}
          </span>
        </>
      }
      triggerClassName={styles.tile}
      triggerTestId="operator-menu"
      menuTestId="operator-menu-panel"
      focus="first-item"
      // Mandatory in the top bar — see the module note.
      portal={align === "end"}
      classes={{
        wrap: `${help.wrap} ${styles.wrap}`,
        panel: `${help.panel} ${align === "start" ? help.start : ""}`,
        items: help.items,
      }}
      head={
        <div className={styles.who} data-testid="operator-who">
          <div className={styles.whoName}>{label}</div>
          <div className={styles.whoSub}>
            {signedIn
              ? "Signed in · single operator"
              : "No login on this install"}
          </div>
        </div>
      }
    >
      {(close) => (
        <>
          <Link
            role="menuitem"
            className={help.item}
            to={settingsPath()}
            state={{ returnTo: location.pathname }}
            onClick={() => {
              close();
              onNavigate();
            }}
          >
            <span className={help.icon} aria-hidden="true">
              <SettingsIcon size={15} />
            </span>
            Settings
          </Link>
          <Link
            role="menuitem"
            className={help.item}
            to={settingsPath("security")}
            onClick={() => {
              close();
              onNavigate();
            }}
          >
            <span className={help.icon} aria-hidden="true">
              <ShieldCheck size={15} />
            </span>
            Security &amp; 2FA
          </Link>
          {/* HELP (#1085), what the `?` menu held — the tour, the manual, What's new. */}
          {onTour ? (
            <button
              type="button"
              role="menuitem"
              className={`${help.item} ${styles.groupStart}`}
              onClick={() => {
                close();
                onNavigate();
                onTour();
              }}
            >
              <span className={help.icon} aria-hidden="true">
                <Compass size={15} />
              </span>
              Intro tour
            </button>
          ) : null}
          <a
            role="menuitem"
            className={onTour ? help.item : `${help.item} ${styles.groupStart}`}
            href={DOCS_HOME_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Documentation (opens in a new tab)"
            onClick={close}
          >
            <span className={help.icon} aria-hidden="true">
              <BookOpen size={15} />
            </span>
            <span className={help.label}>
              Documentation
              <span className={help.sub}>{DOCS_HOST}</span>
            </span>
            <span className={help.trail} aria-hidden="true">
              <ExternalLink size={13} />
            </span>
          </a>
          {onWhatsNew && whatsNewLabel ? (
            <button
              type="button"
              role="menuitem"
              className={help.item}
              onClick={() => {
                close();
                onNavigate();
                onWhatsNew();
              }}
            >
              <span className={help.icon} aria-hidden="true">
                <Sparkles size={15} />
              </span>
              {whatsNewLabel}
            </button>
          ) : null}
          {signedIn ? (
            <button
              type="button"
              role="menuitem"
              className={`${help.item} ${styles.groupStart}`}
              onClick={() => {
                close();
                // `logout` hard-navigates to /login on success; a failure leaves the operator
                // where they are, which the Settings button already handles the same way.
                api.logout().catch(() => {});
              }}
              data-testid="operator-sign-out"
            >
              <span className={help.icon} aria-hidden="true">
                <LogOut size={15} />
              </span>
              Sign out
            </button>
          ) : null}
        </>
      )}
    </AnchoredMenu>
  );
}
