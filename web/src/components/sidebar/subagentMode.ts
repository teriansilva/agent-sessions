/** How the sidebar shows subagent sessions (#2325): a device-local preference, like the map's
 *  grouping mode. `nested` (default) lists a main session's subagents under it, collapsed until
 *  opened; `hidden` lists none except the ones that need the operator. Neither changes what
 *  search or filters match — those only ever match main sessions. */
import { useCallback, useEffect, useState } from "react";

export type SubagentMode = "nested" | "hidden";

export const SUBAGENT_MODE_KEY = "tr-sidebar-subagents";
const EVENT = "agent-sessions:subagent-mode";

/** The choice made on this page when storage refused to keep it (private mode, quota). */
let pageOnly: SubagentMode | null = null;

export function readSubagentMode(): SubagentMode {
  if (pageOnly !== null) return pageOnly;
  try {
    return localStorage.getItem(SUBAGENT_MODE_KEY) === "hidden" ? "hidden" : "nested";
  } catch {
    return "nested";
  }
}

export function writeSubagentMode(mode: SubagentMode): void {
  try {
    localStorage.setItem(SUBAGENT_MODE_KEY, mode);
    pageOnly = null;
  } catch {
    pageOnly = mode;
  }
  window.dispatchEvent(new Event(EVENT));
}

/** The preference, kept in step across the sidebar, other panes and tabs. */
export function useSubagentMode(): [SubagentMode, (mode: SubagentMode) => void] {
  const [mode, setMode] = useState(readSubagentMode);
  useEffect(() => {
    const refresh = () => setMode(readSubagentMode());
    const onStorage = (e: StorageEvent) => {
      if (e.key === null || e.key === SUBAGENT_MODE_KEY) refresh();
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener(EVENT, refresh);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(EVENT, refresh);
    };
  }, []);
  const set = useCallback((value: SubagentMode) => {
    setMode(value);
    writeSubagentMode(value);
  }, []);
  return [mode, set];
}
