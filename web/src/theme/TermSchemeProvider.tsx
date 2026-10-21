import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useConfig } from "../app/config";
import { api } from "../lib/api";
import { TERM_SCHEME_STORAGE_KEY, TermSchemeCtx } from "./termSchemeStore";
import {
  coerceTermScheme,
  DEFAULT_TERM_SCHEME,
  isTermSchemeId,
  resolveTermScheme,
  type TermSchemeId,
} from "./termSchemes";
import { useLatestWinsSave } from "./latestWinsSave";
import { useTheme } from "./themeStore";

function readStored(): TermSchemeId {
  try {
    return coerceTermScheme(localStorage.getItem(TERM_SCHEME_STORAGE_KEY));
  } catch {
    return DEFAULT_TERM_SCHEME;
  }
}

function store(id: TermSchemeId): void {
  try {
    localStorage.setItem(TERM_SCHEME_STORAGE_KEY, id);
  } catch {
    /* storage disabled — the choice still applies for this page */
  }
}

/** Owns the terminal colour scheme (#1494 P2), with the same contract as the accent and the
 *  font: the device cache wins, `/api/config` seeds a device that has no valid choice once, and a
 *  change is cached locally and persisted best-effort.
 *
 *  It also owns `--term-bg` while a non-default scheme is chosen. That token is the ground every
 *  terminal-shaped surface paints (the wrapper slack below the last xterm row, #473, the session
 *  window, the template previews), so it must equal the canvas the scheme paints — set inline on
 *  <html> from the RESOLVED mode, exactly as the accent overrides `--accent`. BattleLab removes
 *  the override, leaving tokens.css in charge: the default stays pixel-identical. */
export function TermSchemeProvider({ children }: { children: ReactNode }) {
  const config = useConfig();
  const { theme } = useTheme();
  const [scheme, setSchemeState] = useState<TermSchemeId>(readStored);
  const reconciled = useRef(false);

  useEffect(() => {
    const root = document.documentElement;
    if (scheme === DEFAULT_TERM_SCHEME) root.style.removeProperty("--term-bg");
    else root.style.setProperty("--term-bg", resolveTermScheme(scheme, theme).background);
  }, [scheme, theme]);

  useEffect(() => {
    if (reconciled.current || !config?.term_scheme) return;
    reconciled.current = true;
    let hasValidLocal = false;
    try {
      hasValidLocal = isTermSchemeId(localStorage.getItem(TERM_SCHEME_STORAGE_KEY));
    } catch {
      /* storage disabled — treat as no local choice */
    }
    if (hasValidLocal) return;
    const server = coerceTermScheme(config.term_scheme);
    setSchemeState(server);
    store(server);
  }, [config?.term_scheme]);

  // Serialized, latest-wins: rapid picks must not let an older request settle last (#1498 review).
  const save = useLatestWinsSave<TermSchemeId>(api.setTermScheme);
  const setScheme = useCallback(
    (id: TermSchemeId) => {
      const next = coerceTermScheme(id);
      setSchemeState(next);
      store(next);
      save(next);
    },
    [save],
  );

  return (
    <TermSchemeCtx.Provider value={{ scheme, setScheme }}>{children}</TermSchemeCtx.Provider>
  );
}
