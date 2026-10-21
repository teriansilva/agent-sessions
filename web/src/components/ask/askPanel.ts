/** The Ask sidebar's open state, shared by the shell (#1294).
 *
 *  Ask is not a route any more: it is the right-hand sidebar the corner icon beside the bell opens,
 *  on every route. The shell owns the state; the icon toggles it, the dashboard's Ask button and
 *  the `/ask` redirect open it, and `AskSidebar` renders it. Its own module (no component) so the
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
