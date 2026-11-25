import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useConfig } from "../app/config";
import { api } from "../lib/api";
import {
  coerceTermFontSize,
  DEFAULT_TERM_FONT_SIZE,
  hasStoredTermFontSize,
  readStoredTermFontSize,
  storeTermFontSize,
} from "./termSize";
import { TermSizeCtx } from "./termSizeStore";

/** Owns the terminal text size (#859). Mirrors AccentProvider exactly, and the precedence is
 *  the point rather than an implementation detail: the **device cache wins**, and the server
 *  value seeds only when this device has no valid choice yet.
 *
 *  That is what makes "per device" fall out for free — a phone parked at 10 px is never
 *  overwritten by the desktop's 13 px on the next reload — while a brand-new device still
 *  inherits something sane instead of starting at the hard default. `setSize` applies +
 *  caches locally and best-effort persists to the server. Must render inside <ConfigProvider>. */
export function TermSizeProvider({ children }: { children: ReactNode }) {
  const config = useConfig();
  const [size, setSizeState] = useState<number>(() => readStoredTermFontSize());
  const reconciled = useRef(false);

  // One-time seed from the server when no *valid* local choice exists yet.
  useEffect(() => {
    if (reconciled.current || config?.term_font_size === undefined) return;
    reconciled.current = true;
    if (hasStoredTermFontSize()) return; // explicit, valid local choice wins
    const server = coerceTermFontSize(config.term_font_size);
    // Seeding from /api/config is the one thing this cannot do during render: the value only
    // exists once an async fetch has landed, and the seed also WRITES to localStorage, which a
    // render is not allowed to do. `reconciled` makes it strictly one cascade per app load, and
    // only on a device that has no choice of its own — the same shape as OverviewPrefsContext
    // and AccentProvider, which seed the same way from the same payload.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSizeState(server);
    storeTermFontSize(server); // also overwrites an invalid cache, if any
  }, [config?.term_font_size]);

  // Server writes are SERIALIZED, never fired per tap (#859 review). Stepping 13 -> 9 fires four
  // independent POSTs, and nothing orders them: HTTP arrival and the server's file lock are both
  // free to interleave, so the 12 px write could settle last and leave the server holding 12
  // while this device shows 9. The device would look right and the next NEW device would be
  // seeded wrong — a bug that is invisible exactly where it happens.
  //
  // At most one request is in flight; the newest value queues behind it and any value it
  // superseded is simply dropped, since only the latest matters. That makes the last write the
  // server sees the last value the operator chose, whatever the network does with the others.
  const pendingRef = useRef<number | null>(null);
  const drainingRef = useRef(false);

  const setSize = useCallback((next: number) => {
    const v = coerceTermFontSize(next);
    setSizeState(v);
    storeTermFontSize(v);

    // The local apply above is the operator's answer and is already done. What follows only
    // decides what the SERVER ends up holding — i.e. what a brand-new device gets seeded with.
    pendingRef.current = v;
    if (drainingRef.current) return; // a drain is running; it will pick this value up
    drainingRef.current = true;
    void (async () => {
      try {
        // One request in flight at a time, always carrying the LATEST value; anything it
        // superseded is dropped, because only the newest size matters.
        //
        // Firing a POST per tap instead would leave the outcome to the network: stepping
        // 13 -> 9 sends four writes, nothing orders their arrival or their turn at the
        // server's file lock, and if the 12 px write settles last the server holds 12 while
        // this device shows 9. The device looks correct and the next new device is seeded
        // wrong — a bug invisible exactly where it happens.
        while (pendingRef.current !== null) {
          const value = pendingRef.current;
          pendingRef.current = null;
          try {
            await api.setTermFontSize(value);
          } catch {
            // Best-effort: a failed persist still applies locally; it just won't follow devices.
          }
        }
      } finally {
        // No await separates the loop's final check from this line, and JS is single-threaded,
        // so a setSize cannot slip in between and be stranded by the flag flipping to false.
        drainingRef.current = false;
      }
    })();
  }, []);

  return (
    <TermSizeCtx.Provider value={{ size, setSize }}>
      {children}
    </TermSizeCtx.Provider>
  );
}

export { DEFAULT_TERM_FONT_SIZE };
