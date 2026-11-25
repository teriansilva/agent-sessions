import { Globe, Share2 } from "lucide-react";
import { PeerGlyph } from "../federation/PeerBadge";
import type { RowMenuEntry } from "../sidebar/RowMenu";
import type { HeadAction } from "../terminal/HeadActions";

/** Share (#2173): ONE entry with two choices, Public and Peer, built here for every surface that
 *  offers it: the terminal and API pane heads (a chip with a menu, or a labelled group once folded)
 *  and the session ⋯ menu (sidebar row, map chip, map window). One module, so the names, their
 *  order and the reason Public is off cannot drift between a header and a menu. */

/** What is being shared: an API session's conversation, or a terminal session's screen. */
export type ShareSubject = "api" | "terminal";

export const SHARE_LABEL = "Share";
/** Public links cover API sessions only (#1404), so a terminal lists Public with this reason. */
export const SHARE_PUBLIC_UNAVAILABLE = "Not available for terminal sessions";

const PUBLIC_LABEL = "Public…";
const PEER_LABEL = "Peer…";
const PUBLIC_ARIA = "Share this session publicly";
const peerAria = (subject: ShareSubject) =>
  subject === "terminal"
    ? "Share this terminal's screen with a paired instance"
    : "Share this session with a paired instance";

/** The pane head's `share` action. `onPublic` is never called for a terminal: its Public item is
 *  listed, disabled, with the reason. Each handler gets the element focus returns to. */
export function shareHeadAction({
  subject,
  onPublic,
  onPeer,
  menuFirst,
}: {
  subject: ShareSubject;
  onPublic?: (trigger: HTMLElement | null) => void;
  onPeer: (trigger: HTMLElement | null) => void;
  menuFirst?: boolean;
}): HeadAction {
  const terminal = subject === "terminal";
  const opener = (trigger?: HTMLElement | null) =>
    trigger ?? (document.activeElement as HTMLElement | null);
  return {
    id: "share",
    label: SHARE_LABEL,
    aria: "Share this session",
    title: "Share: a public link, or one paired BattleLab instance",
    icon: <Share2 size={13} aria-hidden="true" />,
    menuFirst,
    // Never runs: an action with `menu` opens its menu (a chip) or is drawn as a group (folded).
    run: () => {},
    menu: [
      {
        id: "share-public",
        label: PUBLIC_LABEL,
        aria: PUBLIC_ARIA,
        title: terminal
          ? SHARE_PUBLIC_UNAVAILABLE
          : "Public: a link anyone can open without a BattleLab account — view only or able to send, with an optional password and expiry",
        icon: <Globe size={13} aria-hidden="true" />,
        disabled: terminal || !onPublic,
        hint: terminal ? SHARE_PUBLIC_UNAVAILABLE : undefined,
        run: (trigger) => {
          if (!terminal) onPublic?.(opener(trigger));
        },
      },
      {
        id: "share-peer",
        label: PEER_LABEL,
        aria: peerAria(subject),
        title: terminal
          ? "Peer: let one paired BattleLab instance watch this terminal's screen as text. View only, with an expiry"
          : "Peer: let one paired BattleLab instance read this session — view only, with an expiry",
        icon: <PeerGlyph size={13} />,
        run: (trigger) => onPeer(opener(trigger)),
      },
    ],
  };
}

/** The session ⋯ menu's Share group: its label, then the same two entries under the same keys.
 *  Empty when `omit` drops both (a window whose pane already offers `share`, #1329). */
export function shareMenuEntries({
  subject,
  onPublic,
  onPeer,
  omit,
}: {
  subject: ShareSubject;
  onPublic: () => void;
  onPeer: () => void;
  omit?: ReadonlySet<string>;
}): RowMenuEntry[] {
  const terminal = subject === "terminal";
  const entries: RowMenuEntry[] = [];
  if (!omit?.has("share-public")) {
    entries.push({
      key: "share-public",
      label: PUBLIC_LABEL,
      ariaLabel: PUBLIC_ARIA,
      icon: <Globe size={15} />,
      disabled: terminal,
      hint: terminal ? SHARE_PUBLIC_UNAVAILABLE : undefined,
      onSelect: onPublic,
    });
  }
  if (!omit?.has("share-peer")) {
    entries.push({
      key: "share-peer",
      label: PEER_LABEL,
      ariaLabel: peerAria(subject),
      icon: <PeerGlyph size={15} />,
      onSelect: onPeer,
    });
  }
  return entries.length ? [{ group: SHARE_LABEL }, ...entries] : [];
}
