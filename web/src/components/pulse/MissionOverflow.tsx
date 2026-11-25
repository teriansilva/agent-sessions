/** The mission's secondary lifecycle, behind one `⋯` (#942).
 *
 *  The console used to render four lifecycle controls side by side at near-equal weight, two of
 *  them destructive and red. That is four shouts and no primary: the operator's eye has nowhere to
 *  land, and the two most dangerous actions compete with the one they almost always want.
 *
 *  So the state's own next step stays inline and everything else collapses here. Nothing is
 *  removed and nothing loses its confirmation — the two-tap CONFIRM path each destructive action
 *  already owns is unchanged, and every button keeps its `data-testid` and its label. Only
 *  prominence changes.
 *
 *  The menu itself — `role="menu"`, arrow keys, Escape, outside press, Tab leaves — is the shared
 *  `AnchoredMenu` (#987), extracted from this file so the Help menu is not a second copy of it.
 *  Focus lands on the menu WRAPPER rather than the first button, because which button is first
 *  depends on the mission's state — and focusing "whatever happens to be first" would land on a
 *  destructive action in some states and not others.
 */
import { Ellipsis } from "lucide-react";

import action from "../ui/actionButton.module.css";
import { AnchoredMenu } from "../ui/AnchoredMenu";
import styles from "./mission.module.css";

export function MissionOverflow({
  busy,
  children,
  note,
}: {
  busy?: boolean;
  children: React.ReactNode;
  /** A sentence under the items — the consequence of the confirmation an item is waiting on (#967).
   *  It sits in the panel but OUTSIDE `role="menu"`, whose children may only be menu items. */
  note?: React.ReactNode;
}) {
  return (
    <AnchoredMenu
      label="More mission actions"
      // The shared 44×44 ghost icon button (#967), and a drawn glyph rather than the "⋯" character,
      // whose width and baseline were the font's to decide.
      trigger={<Ellipsis size={18} aria-hidden="true" />}
      triggerClassName={action.icon}
      triggerTestId="mission-overflow"
      menuTestId="mission-overflow-menu"
      disabled={busy}
      focus="menu"
      classes={{
        wrap: styles.overflowWrap,
        panel: styles.overflowMenu,
        items: styles.overflowItems,
      }}
      note={note}
    >
      {children}
    </AnchoredMenu>
  );
}
