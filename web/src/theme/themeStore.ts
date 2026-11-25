import { createContext, useContext } from "react";
import { DEFAULT_THEME, type ThemeChoice, type ThemeId } from "./themes";

export interface ThemeStore {
  /** The mode in force — always `dark` or `light`. What every consumer that paints wants. */
  theme: ThemeId;
  /** What the operator chose (#1494), which may be `system`. Only the picker needs it. */
  choice: ThemeChoice;
  setTheme: (choice: ThemeChoice) => void;
}

/** Current theme + setter. Default value is inert (dark, no-op setter) so a consumer
 *  rendered outside the provider degrades gracefully rather than throwing. */
export const ThemeCtx = createContext<ThemeStore>({
  theme: DEFAULT_THEME,
  choice: DEFAULT_THEME,
  setTheme: () => {},
});

export function useTheme(): ThemeStore {
  return useContext(ThemeCtx);
}
