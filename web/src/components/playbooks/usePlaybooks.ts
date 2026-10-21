import { useCallback, useEffect, useRef, useState } from "react";

/** Keep the last successful read visible on failure; only the newest read may publish. */
export function usePlaybookRead<T>(read: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const sequence = useRef(0);
  const reload = useCallback(async () => {
    const current = ++sequence.current;
    setLoading(true);
    try {
      const value = await read();
      if (current === sequence.current) {
        setData(value);
        setError(null);
      }
      // Callers use the return value to clear a stale-write refusal. An older request must
      // not clear it after a newer refresh failed, even though its own HTTP request succeeded.
      return current === sequence.current ? value : null;
    } catch (e) {
      if (current === sequence.current) setError(errorText(e));
      return null;
    } finally {
      if (current === sequence.current) setLoading(false);
    }
  }, [read]);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
    return () => {
      sequence.current += 1;
    };
  }, [reload]);
  return { data, error, loading, reload };
}

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : "The request could not be completed.";
}
