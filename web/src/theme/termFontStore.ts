import { createContext, useContext } from "react";
import { DEFAULT_TERM_FONT_FAMILY } from "./termFont";

export interface TermFontStore {
  /** Always a sane CSS font stack (see `coerceTermFontFamily`). */
  family: string;
  setFamily: (family: string) => void;
}

/** Current terminal font stack + setter. The default value is inert (the default stack, a
 *  no-op setter) so a consumer rendered outside the provider degrades gracefully rather
 *  than throwing — same contract as TermSizeCtx / AccentCtx. */
export const TermFontCtx = createContext<TermFontStore>({
  family: DEFAULT_TERM_FONT_FAMILY,
  setFamily: () => {},
});

export function useTermFont(): TermFontStore {
  return useContext(TermFontCtx);
}
