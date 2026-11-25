import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../../../lib/api";
import type { Debrief } from "../../../types/shelf";

/** While a summary is being written, how often the page asks again. */
export const DEBRIEF_POLL_MS = 3000;

export interface MissionDebrief {
  debrief: Debrief | null;
  error: string | null;
  /** Why the last Regenerate did not write a summary (the server's reason). */
  regenError: string | null;
  regenerating: boolean;
  regenerate: () => Promise<void>;
}

/**
 * The mission debrief (#1740 P2). Read when the page shows it, again whenever `recordsKey`
 * changes (the shelf's revision and the objectives' verdicts: the records moved), and every few
 * seconds while a summary is being written. A read that lands after a newer one is dropped.
 */
export function useMissionDebrief(
  missionId: string,
  enabled: boolean,
  recordsKey: string,
): MissionDebrief {
  const [debrief, setDebrief] = useState<Debrief | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [regenerating, setRegenerating] = useState(false);
  const [regenError, setRegenError] = useState<string | null>(null);
  const gen = useRef(0);

  const read = useCallback(async () => {
    const mine = ++gen.current;
    try {
      const d = await api.missionDebrief(missionId);
      if (mine !== gen.current) return;
      if (!d || !d.facts || typeof d.facts.revision !== "string") {
        throw new Error("not a debrief");
      }
      setDebrief(d);
      setError(null);
    } catch {
      if (mine !== gen.current) return;
      setError("The debrief could not be read. It will try again.");
    }
  }, [missionId]);

  useEffect(() => {
    if (!enabled) return;
    // The first read is an external fetch whose answer sets state after it arrives.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void read();
  }, [enabled, read, recordsKey]);

  const generating = !!debrief?.generating;
  useEffect(() => {
    if (!enabled || !generating) return;
    const t = window.setInterval(() => void read(), DEBRIEF_POLL_MS);
    return () => window.clearInterval(t);
  }, [enabled, generating, read]);

  const regenerate = useCallback(async () => {
    const mine = ++gen.current;
    setRegenerating(true);
    setRegenError(null);
    try {
      const d = await api.regenerateDebrief(missionId);
      if (mine === gen.current && d?.facts) {
        setDebrief(d);
        setError(null);
      }
    } catch (e) {
      setRegenError(
        e instanceof ApiError && e.message
          ? e.message
          : "The summary could not be written. Try again.",
      );
      // The facts are still the records: re-read them so the page is current.
      void read();
    } finally {
      setRegenerating(false);
    }
  }, [missionId, read]);

  return { debrief, error, regenError, regenerating, regenerate };
}
