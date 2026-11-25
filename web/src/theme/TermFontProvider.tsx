import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useConfig } from "../app/config";
import { api } from "../lib/api";
import {
  coerceTermFontFamily,
  DEFAULT_TERM_FONT_FAMILY,
  hasStoredTermFontFamily,
  readStoredTermFontFamily,
  storeTermFontFamily,
} from "./termFont";
import { TermFontCtx } from "./termFontStore";

/** Owns the terminal font FAMILY (#866). Mirrors TermSizeProvider exactly — same precedence,
 *  same serialization — because it is the same kind of value: a per-device rendering choice with
 *  a server-held seed.
 *
 *  The **device cache wins**; the server value seeds only a device that has no valid choice of
 *  its own. That is what makes "the phone at 10 px in one face, the desktop at 13 px in another"
 *  fall out of two independent axes instead of needing a third pref for the combination.
 *  Must render inside <ConfigProvider>. */
export function TermFontProvider({ children }: { children: ReactNode }) {
  const config = useConfig();
  const [family, setFamilyState] = useState<string>(() =>
    readStoredTermFontFamily(),
  );
  const reconciled = useRef(false);

  // One-time seed from the server when no *valid* local choice exists yet.
  useEffect(() => {
    if (reconciled.current || config?.term_font_family === undefined) return;
    reconciled.current = true;
    if (hasStoredTermFontFamily()) return; // explicit, valid local choice wins
    const server = coerceTermFontFamily(config.term_font_family);
    // Seeding from /api/config cannot happen during render: the value only exists once an async
    // fetch has landed, and the seed also WRITES to localStorage, which a render may not do.
    // `reconciled` makes it strictly one cascade per app load, and only on a device with no
    // choice of its own — the same shape as AccentProvider and TermSizeProvider.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setFamilyState(server);
    storeTermFontFamily(server); // also overwrites an invalid cache, if any
  }, [config?.term_font_family]);

  // Server writes are SERIALIZED, never fired per interaction (#859's finding, and it applies
  // verbatim here): typing in the custom-stack field can produce a write per keystroke, and
  // nothing orders independent POSTs. If an intermediate value settles last, the server holds a
  // stack the operator never finished typing — this device would look right while the next NEW
  // device got seeded wrong, which is a bug invisible exactly where it happens.
  //
  // At most one request is in flight; the newest value queues behind it and anything it
  // superseded is dropped, since only the latest matters.
  const pendingRef = useRef<string | null>(null);
  const drainingRef = useRef(false);

  const setFamily = useCallback((next: string) => {
    const v = coerceTermFontFamily(next);
    setFamilyState(v);
    storeTermFontFamily(v);

    // The local apply above is the operator's answer and is already done. What follows only
    // decides what the SERVER ends up holding — i.e. what a brand-new device is seeded with.
    pendingRef.current = v;
    if (drainingRef.current) return; // a drain is running; it will pick this value up
    drainingRef.current = true;
    void (async () => {
      try {
        while (pendingRef.current !== null) {
          const value = pendingRef.current;
          pendingRef.current = null;
          try {
            await api.setTermFontFamily(value);
          } catch {
            // Best-effort: a failed persist still applies locally; it just won't follow devices.
          }
        }
      } finally {
        // No await separates the loop's final check from this line, and JS is single-threaded,
        // so a setFamily cannot slip in between and be stranded by the flag flipping to false.
        drainingRef.current = false;
      }
    })();
  }, []);

  return (
    <TermFontCtx.Provider value={{ family, setFamily }}>
      {children}
    </TermFontCtx.Provider>
  );
}

export { DEFAULT_TERM_FONT_FAMILY };
