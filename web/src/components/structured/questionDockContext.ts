/** The pinned question dock's contexts (#2234), apart from its components for fast refresh. */
import { createContext } from "react";

/** Where the pane's dock is in the DOM; `null` before it mounts (and outside a pane). */
export const QuestionDockTarget = createContext<HTMLElement | null>(null);

/** A docked question's minimise state, read by `ChoiceQuestions` for its head toggle. */
export const DockMinimise = createContext<{ minimised: boolean; toggle: () => void } | null>(null);
