import { createContext, useContext } from "react";
import { DEFAULT_THEME, type ThemeId } from "./themes";

export interface ThemeStore {
  theme: ThemeId;
  setTheme: (id: ThemeId) => void;
}

/** Current theme + setter. Default value is inert (dark, no-op setter) so a consumer
 *  rendered outside the provider degrades gracefully rather than throwing. */
export const ThemeCtx = createContext<ThemeStore>({
  theme: DEFAULT_THEME,
  setTheme: () => {},
});

export function useTheme(): ThemeStore {
  return useContext(ThemeCtx);
}
