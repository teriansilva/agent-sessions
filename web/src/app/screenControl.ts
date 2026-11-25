/** HQ's screen executor, shared above the router (#2102). Its own module (no component) so
 *  `ScreenContext.tsx` exports a component only and keeps Fast Refresh.
 *
 *  Two readers: whatever DELIVERS a live directive (the HQ console's stream, the call socket)
 *  takes the executor and calls `execute`; whatever RENDERS a turn reads this device's outcomes
 *  to word the line under the answer. Outside the provider (a unit test rendering one component)
 *  there is no executor: nothing runs, and every stored request reads as "Asked to show". */
import { createContext, useContext, useSyncExternalStore } from "react";
import type { Outcomes, ScreenExecutor, ScreenSource } from "./screenExecutor";

export const ScreenCtx = createContext<ScreenExecutor | null>(null);

export function useScreen(): ScreenExecutor | null {
  return useContext(ScreenCtx);
}

const NONE: Outcomes = new Map();
const noSubscribe = () => () => undefined;
const noOutcomes = () => NONE;

/** What this device did with each directive it executed. Memory only. */
export function useScreenOutcomes(): Outcomes {
  const executor = useContext(ScreenCtx);
  return useSyncExternalStore(
    executor ? executor.subscribe : noSubscribe,
    executor ? executor.snapshot : noOutcomes,
  );
}

/** Fired on `window` when a directive put something on screen, or when the page it tried to
 *  leave is asking about unsaved changes. HQ's drawer closes on a phone (as it does for a tapped
 *  link in an answer) and the call minimises to its bar on a phone: both cover the page. */
export const SCREEN_SHOWN_EVENT = "hq:screen-shown";
export interface ScreenShownDetail {
  source: ScreenSource;
}
