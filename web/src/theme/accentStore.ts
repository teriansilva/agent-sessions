import { createContext, useContext } from "react";
import { DEFAULT_ACCENT } from "./accent";

export interface AccentStore {
  accent: string; // normalized #rrggbb
  setAccent: (hex: string) => void;
}

/** Current brand accent + setter. Default value is inert (the default accent, no-op setter)
 *  so a consumer rendered outside the provider degrades gracefully rather than throwing. */
export const AccentCtx = createContext<AccentStore>({
  accent: DEFAULT_ACCENT,
  setAccent: () => {},
});

export function useAccent(): AccentStore {
  return useContext(AccentCtx);
}
