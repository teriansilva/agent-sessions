import { useCallback, useEffect, useRef } from "react";

/** Which answers may still change a component's state (#2055 review).
 *
 *  The pattern this replaces, a `useRef(true)` set false in an effect cleanup and never set true
 *  again, is dead after React StrictMode's effect replay (in development a component is mounted,
 *  cleaned up and mounted again, `web/src/main.tsx`): every answer after it was discarded, so the
 *  share dialog never finished loading. This fence is armed in the effect BODY, so the replay
 *  arms it again, and it is a fresh token per arming, so it still drops what must be dropped:
 *
 *  - an answer for an earlier `key` (another session, another peer): the token changed;
 *  - an answer that arrives after the component was closed: the token was cleared;
 *  - an answer to a request the replayed first mount started: that token was replaced.
 *
 *  `ticket()` is taken when a request STARTS; the function it returns tells, when the answer
 *  arrives, whether it may be applied. Call this hook BEFORE the effects that start requests:
 *  React runs a component's effects in the order they are declared, so the fence is armed when
 *  they run. */
export function useAnswerFence(key: unknown): () => () => boolean {
  const current = useRef<object | null>(null);
  useEffect(() => {
    const token = {};
    current.current = token;
    return () => {
      if (current.current === token) current.current = null;
    };
  }, [key]);
  return useCallback(() => {
    const token = current.current;
    return () => token !== null && current.current === token;
  }, []);
}
