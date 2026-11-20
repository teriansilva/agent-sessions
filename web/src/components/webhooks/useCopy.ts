/** Copy a value to the clipboard, or — where there is no clipboard (an insecure origin, a denied
 *  permission) — select its element so the operator can copy it by hand. `which` names what was
 *  copied last, so a panel with two Copy buttons says which one worked. */
import { useCallback, useState, type RefObject } from "react";

export type CopyState = { which: string; ok: boolean } | null;

export function useCopy(): [CopyState, (which: string, text: string, el: RefObject<HTMLElement | null>) => Promise<void>] {
  const [state, setState] = useState<CopyState>(null);
  const copy = useCallback(
    async (which: string, text: string, el: RefObject<HTMLElement | null>) => {
      try {
        await navigator.clipboard.writeText(text);
        setState({ which, ok: true });
      } catch {
        if (el.current) window.getSelection()?.selectAllChildren(el.current);
        setState({ which, ok: false });
      }
    },
    [],
  );
  return [state, copy];
}
