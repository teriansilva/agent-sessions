import { createContext, useContext } from "react";
import { DEFAULT_TERM_SCHEME, type TermSchemeId } from "./termSchemes";

export const TERM_SCHEME_STORAGE_KEY = "tr-termscheme";

export interface TermSchemeStore {
  scheme: TermSchemeId;
  setScheme: (id: TermSchemeId) => void;
}

/** The terminal colour scheme (#1494 P2). Inert default (BattleLab, no-op setter) so a terminal
 *  rendered outside the provider paints today's colours rather than throwing. */
export const TermSchemeCtx = createContext<TermSchemeStore>({
  scheme: DEFAULT_TERM_SCHEME,
  setScheme: () => {},
});

export function useTermScheme(): TermSchemeStore {
  return useContext(TermSchemeCtx);
}
