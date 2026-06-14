import { createContext, useContext } from "react";
import type { Session } from "../types/api";

/** Shared store of the sessions the sidebar has loaded, so the compact desktop/mobile
 *  header can resolve the *current* session's title (matched by engine+uuid from the URL)
 *  without re-fetching. The sidebar (the single owner of the list data) publishes its rows
 *  here; the header is a read-only consumer. Empty until the sidebar's first page lands. */
export interface SessionsStore {
  sessions: Session[];
  setSessions: (s: Session[]) => void;
}

export const SessionsCtx = createContext<SessionsStore>({ sessions: [], setSessions: () => {} });

export function useSessionsStore(): SessionsStore {
  return useContext(SessionsCtx);
}
