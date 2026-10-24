/** The relay connection as a plate in the top bar (#1658).
 *
 *  Streamed through the Home Free relay, the connect shell used to float its own connection bar
 *  over the middle of this bar (#684 made it collapsible, but it still floated, and nothing said
 *  its LED was a control). Now the shell publishes the connection (`homefree/relayStatus`) and the
 *  bar shows it here, left of HQ, at every width (`data-topbar-keep`). Outside the relay nothing
 *  is published and this renders nothing.
 *
 *  **LED-only.** The top bar carries no readouts (docs/design.md, #1085), so the plate is the
 *  HQ/bell plate with one LED: green breathing = linked, amber blinking = the session ends soon.
 *  The colour never speaks alone — the state word is in the trigger's name and tooltip, and in the
 *  panel. A lost link has no plate state: `closed` / `expired` tear the app down and the connect
 *  card says why.
 *
 *  **The panel is `AnchoredMenu`** (the operator menu's shape): a status header and one item, Sign
 *  out. Portalled on desktop (a panel anchored inside `.hud-topbar` is painted over by the pane,
 *  #752 / #987), the bottom sheet on a phone. Sign out is the SHELL's path (`relaySignOut`), which
 *  tears this app down — so it runs after the menu's own close, outside React's event dispatch.
 *
 *  **Docking** tells the shell its floating bar can stand down. Only a rendered plate docks. */
import { LogOut } from "lucide-react";
import { useEffect, useId, useSyncExternalStore } from "react";

import {
  dockRelayPlate,
  getRelaySnapshot,
  minutesLeft,
  relaySignOut,
  subscribeRelay,
  type RelaySnapshot,
} from "../../homefree/relayStatus";
import bell from "../pulse/NotificationBell.module.css";
import { AnchoredMenu } from "../ui/AnchoredMenu";
import help from "./HelpMenu.module.css";
import styles from "./RelayChip.module.css";

const getServerSnapshot = () => null;

function stateWord(snap: RelaySnapshot): string {
  return snap.link === "ending" ? "session ends soon" : "linked";
}

/** "Relay connection: linked — onyx-9780" — the plate's NAME. No countdown in it: focus parks on
 *  this plate, and a name that changed every second would be re-announced every second (#1662
 *  review). The time left is the plate's DESCRIPTION and tooltip instead, to the minute. */
function relayPlateName(snap: RelaySnapshot): string {
  return `Relay connection: ${stateWord(snap)} — ${snap.name}`;
}

export function RelayChip() {
  const snap = useSyncExternalStore(subscribeRelay, getRelaySnapshot, getServerSnapshot);
  const headId = useId();
  const live = snap !== null;
  useEffect(() => (live ? dockRelayPlate() : undefined), [live]);
  if (!snap) return null;

  const name = relayPlateName(snap);
  const left = minutesLeft(snap.remaining);
  const led = snap.link === "ending" ? "attention" : "up";
  return (
    <span className={styles.slot} data-topbar-keep="" data-relay-plate="">
      {/* The plate's description: hidden, yet still read through aria-describedby. */}
      <span id={`${headId}-left`} hidden>
        {left}
      </span>
      <AnchoredMenu
        label="Relay connection"
        triggerLabel={name}
        triggerTitle={left ? `${name} · ${left}` : name}
        triggerDescribedBy={left ? `${headId}-left` : undefined}
        trigger={<span className={`hud-led ${led}`} aria-hidden="true" data-testid="relay-led" />}
        triggerClassName={bell.btn}
        triggerTestId="relay-plate"
        menuTestId="relay-panel"
        focus="first-item"
        // Mandatory in the top bar — see the module note.
        portal
        classes={{ wrap: `${help.wrap} ${bell.wrap}`, panel: help.panel, items: help.items }}
        head={
          // Sign out is described by this header, so a screen reader hears the state, the status
          // line and the time left when focus lands on the one item.
          <div className={styles.head} id={headId} data-testid="relay-head">
            <div className={styles.state}>
              <span className={`hud-led ${led}`} aria-hidden="true" />
              <span data-testid="relay-state">
                {stateWord(snap)} — {snap.name}
              </span>
            </div>
            <div className={styles.status} data-testid="relay-status">
              {snap.status}
            </div>
            {snap.remaining ? (
              <div className={styles.clock}>
                Session <b>{snap.remaining}</b> left
              </div>
            ) : null}
          </div>
        }
      >
        {(close) => (
          <button
            type="button"
            role="menuitem"
            className={help.item}
            onClick={() => {
              close();
              // The shell's Sign out unmounts this app; never from inside React's own dispatch.
              window.setTimeout(relaySignOut, 0);
            }}
            aria-describedby={headId}
            data-testid="relay-sign-out"
          >
            <span className={help.icon} aria-hidden="true">
              <LogOut size={15} />
            </span>
            Sign out
          </button>
        )}
      </AnchoredMenu>
    </span>
  );
}
