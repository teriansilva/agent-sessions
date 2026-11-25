import type { RefObject } from "react";
import type { TermStatus } from "../../lib/termSocket";
import type { HeadAction } from "./HeadActions";

/** What a session's host — the full-screen route (`SessionView`) or a map window
 *  (`SessionWindow`) — gives the pane besides its identity. The terminal takes these as its own
 *  props; a pane `RuntimeGate` picks INSTEAD of the terminal (the structured API pane, #1332)
 *  receives the same set through the gate, so it gets the same head actions and Files drawer. */
export interface PaneHost {
  /** The id the URL has settled on, for row lookups and actions (#867). */
  rowKey?: string;
  filesOpen?: boolean;
  filesDisabledReason?: string;
  onToggleFiles?: (trigger?: HTMLElement | null) => void;
  /** Only the full-screen route passes it, and only where a window can be hosted (#936). */
  onToMap?: () => void;
  /** #1109: inside a map window the chrome bar is the pane's ONLY bar. */
  suppressHead?: boolean;
  headActionsSlot?: HTMLElement | null;
  headOverflowRef?: { current: HeadAction[] };
  /** Every action the pane offers, so the chrome's merged ⋯ omits their session twins (#1329). */
  headAllRef?: { current: HeadAction[] };
  headReservePx?: number;
  headBarRef?: RefObject<HTMLElement | null>;
  /** The window chrome's LED reads the pane's connection state through this. */
  onTermStatus?: (s: TermStatus) => void;
}
