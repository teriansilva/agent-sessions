import { useCallback, useEffect, useRef, useState } from "react";
import type { OutboxEntry } from "./outbox";

/** The outbox's state (#1465). The ref is the synchronous truth — a POST is started at most once,
 *  even when an effect runs twice — and the state renders it. */
export function useOutbox() {
  const ref = useRef<OutboxEntry[]>([]);
  const [outbox, setState] = useState<OutboxEntry[]>([]);
  const setOutbox = useCallback((next: (list: OutboxEntry[]) => OutboxEntry[]) => {
    const list = next(ref.current);
    if (list === ref.current) return;
    ref.current = list;
    setState(list);
  }, []);
  return { outbox, outboxRef: ref, setOutbox };
}

/** False once the pane unmounted (a session switch: panes are keyed per session). A late POST
 *  result then touches nothing and sends nothing. */
export function useAlive() {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return alive;
}

/** While anything the server does not have yet is held, a reload or close asks first. In-app
 *  navigation is not intercepted; the note under the pending block says what leaving does. */
export function useLeaveGuard(armed: boolean) {
  useEffect(() => {
    if (!armed) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = ""; // older browsers need it set to show the prompt
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [armed]);
}
