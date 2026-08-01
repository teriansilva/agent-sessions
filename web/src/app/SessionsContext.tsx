import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { Session } from "../types/api";
import { SessionsCtx } from "./sessionsStore";

export function SessionsProvider({ children }: { children: ReactNode }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const value = useMemo(() => ({ sessions, setSessions }), [sessions]);
  return <SessionsCtx.Provider value={value}>{children}</SessionsCtx.Provider>;
}
