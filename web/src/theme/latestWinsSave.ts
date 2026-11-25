import { useCallback, useRef } from "react";

/** Serialized, latest-wins server writes for a per-device preference (#1494) — the shape
 *  TermSizeProvider and TermFontProvider already carry inline (#859, #866).
 *
 *  Independent POSTs are not ordered: pick Nord, then Dracula quickly, and if the Nord request
 *  settles LAST the server keeps Nord while this device shows Dracula — invisible here, wrong on
 *  the next new device the server seeds. So at most one request is in flight; the newest value
 *  queues behind it, and anything it superseded is dropped, since only the latest matters.
 *  Failures are swallowed: the local apply already happened, it just won't follow devices. */
export function useLatestWinsSave<T>(send: (value: T) => Promise<unknown>): (value: T) => void {
  const pending = useRef<{ value: T } | null>(null);
  const draining = useRef(false);
  // `send` is a module-level api function in every caller, so this callback is stable.
  return useCallback((value: T) => {
    pending.current = { value };
    if (draining.current) return; // the running drain picks this value up
    draining.current = true;
    void (async () => {
      try {
        while (pending.current !== null) {
          const { value: next } = pending.current;
          pending.current = null;
          try {
            await send(next);
          } catch {
            /* best-effort, see above */
          }
        }
      } finally {
        // No await between the loop's last check and here, so no value can be stranded.
        draining.current = false;
      }
    })();
  }, [send]);
}
