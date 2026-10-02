import DefaultTheme from "vitepress/theme";
import { useData } from "vitepress";
import { watch } from "vue";
import type { Theme } from "vitepress";
import "./hud.css";

// VitePress signals its colour scheme with `html.dark`; the BattleLab palette (web/src/tokens.css,
// imported by hud.css) keys off the bare `:root` for dark and `:root[data-theme="light"]` for
// light — the same contract the app uses. Mirroring VitePress's state onto `data-theme` is what
// lets the docs import the app's palette verbatim instead of restating it in a `.dark` block,
// which is the whole point of the shared tokens file (#829).
export default {
  extends: DefaultTheme,
  setup() {
    const { isDark } = useData();
    const apply = (dark: boolean) => {
      document.documentElement.dataset.theme = dark ? "dark" : "light";
    };
    // Runs client-side only; on the server there is no documentElement to mark, and the
    // pre-hydration inline script VitePress injects has already set `html.dark` by then.
    if (typeof document !== "undefined") {
      apply(isDark.value);
      watch(isDark, apply);
    }
  },
} satisfies Theme;
