import { createContext, useContext } from "react";
import { DEFAULT_TERM_FONT_SIZE } from "./termSize";

export interface TermSizeStore {
  /** Always a valid integer in [TERM_FONT_SIZE_MIN, TERM_FONT_SIZE_MAX]. */
  size: number;
  setSize: (size: number) => void;
}

/** Current terminal font size + setter. The default value is inert (the default size, a
 *  no-op setter) so a consumer rendered outside the provider degrades gracefully rather
 *  than throwing — same contract as AccentCtx. */
export const TermSizeCtx = createContext<TermSizeStore>({
  size: DEFAULT_TERM_FONT_SIZE,
  setSize: () => {},
});

export function useTermSize(): TermSizeStore {
  return useContext(TermSizeCtx);
}
