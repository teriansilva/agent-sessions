/** The action gallery (#2248 P1), read on mount and on `reload()`. The last complete answer is kept
 *  (data only) so the wizard's What step, which mounts again on every visit, draws its list at once
 *  while it reads again. A failed read is an error string, never an empty list: "no actions" and
 *  "couldn't read them" are different answers. */
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../../lib/api";
import { errorWords } from "../../lib/automations";
import type { ActionGallery } from "../../types/actions";

let last: ActionGallery | null = null;

/** Tests only: forget the kept answer. */
export function resetActionsCache(): void {
  last = null;
}

export function useActions(): {
  gallery: ActionGallery | null;
  error: string;
  loading: boolean;
  reload: () => void;
} {
  const [gallery, setGallery] = useState<ActionGallery | null>(last);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const live = useRef(true);
  const load = useCallback(() => {
    setLoading(true);
    api.actions().then(
      (g) => {
        if (!live.current) return;
        const ok: ActionGallery = {
          actions: Array.isArray(g?.actions) ? g.actions : [],
          categories: Array.isArray(g?.categories) ? g.categories : [],
          effects: Array.isArray(g?.effects) ? g.effects : [],
        };
        last = ok;
        setGallery(ok);
        setError("");
        setLoading(false);
      },
      (e: unknown) => {
        if (!live.current) return;
        setError(errorWords(e, "Couldn’t read the actions."));
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
  return { gallery, error, loading, reload: load };
}
