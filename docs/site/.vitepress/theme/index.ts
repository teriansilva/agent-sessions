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
      restoreFocusAfterSearch();
    }
  },
} satisfies Theme;

/** The local search modal (#1597, design.md §8): VitePress moves focus into it on Ctrl K or on
 *  activating the search button, but drops it on the body when Esc closes it, so a keyboard reader
 *  loses their place. Remember what had focus when the box mounts, and give it back when the box
 *  goes, if it is still on the page. */
function restoreFocusAfterSearch() {
  let before: HTMLElement | null = null;
  let open = false;
  // Capture before VitePress moves focus into the box: keydown runs ahead of the mount. Any key,
  // not just Ctrl K — Enter or Space on the focused search button opens it too (Hermes 6787).
  document.addEventListener(
    "keydown",
    () => {
      if (!open) before = document.activeElement as HTMLElement | null;
    },
    true,
  );
  document.addEventListener(
    "pointerdown",
    (e) => {
      if (!open && (e.target as Element | null)?.closest?.(".DocSearch-Button"))
        before = (e.target as Element).closest(".DocSearch-Button") as HTMLElement;
    },
    true,
  );
  new MutationObserver(() => {
    const now = !!document.querySelector(".VPLocalSearchBox");
    if (open && !now && before && before.isConnected && before !== document.body) {
      const el = before;
      requestAnimationFrame(() => el.focus({ preventScroll: true }));
    }
    if (!now) before = null;
    open = now;
  }).observe(document.body, { childList: true, subtree: false });
}
