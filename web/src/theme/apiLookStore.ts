import { createContext, useContext } from "react";
import { apiLookProps, DEFAULT_API_LOOK, type ApiLook } from "./apiLook";
import { useAccent } from "./accentStore";
import { useTermFont } from "./termFontStore";
import { useTheme } from "./themeStore";

export const API_LOOK_STORAGE_KEY = "tr-apilook";

export interface ApiLookStore {
  look: ApiLook;
  setLook: (look: ApiLook) => void;
}

/** The API-session look (#1494 P3). Inert default — the default look, no-op setter — so a pane
 *  rendered outside the provider (the public share viewer) paints exactly today's look. */
export const ApiLookCtx = createContext<ApiLookStore>({
  look: DEFAULT_API_LOOK,
  setLook: () => {},
});

export function useApiLook(): ApiLookStore {
  return useContext(ApiLookCtx);
}

/** What a conversation LOG spreads onto its element: `{}` for the default look. */
export function useApiLogProps(override?: ApiLook): ReturnType<typeof apiLookProps> {
  const { look } = useApiLook();
  const { theme } = useTheme();
  const { family } = useTermFont();
  const { accent } = useAccent();
  return apiLookProps(override ?? look, theme, family, accent);
}
