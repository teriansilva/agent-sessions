/** The webhook list (#2221), read once on mount and again on `reload()`. A failed read is an error
 *  string, never an empty list: "no webhooks" and "couldn't read them" are different answers. */
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../../lib/api";
import { errorWords } from "../../lib/automations";
import type { Webhook } from "../../types/webhooks";

export function useWebhooks(): {
  list: Webhook[] | null;
  error: string;
  loading: boolean;
  reload: () => void;
} {
  const [list, setList] = useState<Webhook[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const live = useRef(true);
  const load = useCallback(() => {
    setLoading(true);
    api.webhooks().then(
      (r) => {
        if (!live.current) return;
        setList(Array.isArray(r?.webhooks) ? r.webhooks : []);
        setError("");
        setLoading(false);
      },
      (e: unknown) => {
        if (!live.current) return;
        setError(errorWords(e, "Couldn’t read the webhooks."));
        setLoading(false);
      },
    );
  }, []);
  useEffect(() => {
    live.current = true;
    // The read's state lands after its await, never synchronously in this body.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
    return () => {
      live.current = false;
    };
  }, [load]);
  return { list, error, loading, reload: load };
}
