import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useConfig } from "../app/config";
import { api } from "../lib/api";
import { coerceApiLook, DEFAULT_API_LOOK, type ApiLook } from "./apiLook";
import { API_LOOK_STORAGE_KEY, ApiLookCtx } from "./apiLookStore";
import { useLatestWinsSave } from "./latestWinsSave";

function readStored(): { look: ApiLook; present: boolean } {
  try {
    const raw = localStorage.getItem(API_LOOK_STORAGE_KEY);
    if (raw === null) return { look: { ...DEFAULT_API_LOOK }, present: false };
    // A cache that isn't JSON is not a choice: it reads as the default and lets the server seed.
    const parsed: unknown = JSON.parse(raw);
    const ok = !!parsed && typeof parsed === "object" && !Array.isArray(parsed);
    return { look: coerceApiLook(parsed), present: ok };
  } catch {
    return { look: { ...DEFAULT_API_LOOK }, present: false };
  }
}

function store(look: ApiLook): void {
  try {
    localStorage.setItem(API_LOOK_STORAGE_KEY, JSON.stringify(look));
  } catch {
    /* storage disabled — the look still applies for this page */
  }
}

/** Owns the API-session look (#1494 P3), with the accent/font contract: the device cache wins,
 *  `/api/config` seeds a device that has no choice of its own once, and a change is cached and
 *  persisted best-effort. Reads coerce per field, so a partly-bad cache keeps its good fields. */
export function ApiLookProvider({ children }: { children: ReactNode }) {
  const config = useConfig();
  const [look, setLookState] = useState<ApiLook>(() => readStored().look);
  const reconciled = useRef(false);

  useEffect(() => {
    if (reconciled.current || !config?.api_look) return;
    reconciled.current = true;
    if (readStored().present) return;
    const server = coerceApiLook(config.api_look);
    // A one-time seed from a value that arrives after mount — the same sanctioned exception the
    // font and size providers take.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLookState(server);
    store(server);
  }, [config?.api_look]);

  // Serialized, latest-wins (#1498 review): the stepper and the pickers can fire several saves in a
  // second, and an older one settling last would seed new devices with a look nobody chose.
  const save = useLatestWinsSave<ApiLook>(api.setApiLook);
  const setLook = useCallback(
    (next: ApiLook) => {
      const clean = coerceApiLook(next);
      setLookState(clean);
      store(clean);
      save(clean);
    },
    [save],
  );

  return <ApiLookCtx.Provider value={{ look, setLook }}>{children}</ApiLookCtx.Provider>;
}
