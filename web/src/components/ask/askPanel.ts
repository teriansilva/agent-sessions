/** The Ask sidebar's open state, shared by the shell (#1294).
 *
 *  HQ is the right-hand sidebar the corner icon beside the bell opens, on every route but HQ's own
 *  page (`/hq*`, #1776), where the panel stays closed and the icon focuses the page's composer
 *  (`HQ_FOCUS_COMPOSER_EVENT`). The shell owns the state; the icon toggles it, the dashboard's
 *  primary contact opens it, and `AskSidebar` renders it. Its own module (no component) so the
 *  files that render components keep fast-refresh.
 *
 *  `triggerRef` is the corner icon: the mobile drawer returns focus to it on every close path, the
 *  same contract the bell's drawer keeps (`useModalDrawer`). */
import { createContext, useContext, type RefObject } from "react";

export interface AskPanel {
  open: boolean;
  openAsk: () => void;
  close: () => void;
  toggle: () => void;
  triggerRef: RefObject<HTMLButtonElement | null>;
}

/** On HQ's page the corner icon asks the page to take the operator to its composer (#1776). */
export const HQ_FOCUS_COMPOSER_EVENT = "hq:focus-composer";

/** Where HQ's panel is a modal drawer instead of a sidebar beside the page. */
export const ASK_DRAWER_MQ = "(max-width: 800px)";

const noop = () => undefined;

/** Outside a shell (a unit test rendering one route) the panel is simply closed and inert. */
export const AskPanelContext = createContext<AskPanel>({
  open: false,
  openAsk: noop,
  close: noop,
  toggle: noop,
  triggerRef: { current: null },
});

export function useAskPanel(): AskPanel {
  return useContext(AskPanelContext);
}
