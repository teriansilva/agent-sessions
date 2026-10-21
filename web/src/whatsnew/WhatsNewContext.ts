import { createContext, useContext } from "react";

/** Reopen What's new from anywhere inside the shell (#971) — Settings → About uses it. `null`
 *  outside the shell, where there is no dialog to open. */
export const WhatsNewCtx = createContext<(() => void) | null>(null);

export function useOpenWhatsNew(): (() => void) | null {
  return useContext(WhatsNewCtx);
}
