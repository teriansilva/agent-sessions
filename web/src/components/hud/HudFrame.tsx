/** Corner-bracket frame — the `.hud-cnr` primitive (App.css) packaged as a component so
 *  surfaces don't hand-paste the four bracket spans. Drop it inside a `position: relative`
 *  box; it renders the four L-brackets (purely decorative, `aria-hidden`).
 *
 *  `hero` switches to the white, larger brackets — use at most one hero frame per view
 *  (the "you are here" panel), per docs/design.md §6. The brackets and their colours live in
 *  App.css (`.hud-cnr`) — this component never forks that CSS. */
export function HudFrame({ hero = false }: { hero?: boolean }) {
  const cls = hero ? "hud-cnr hero" : "hud-cnr";
  return (
    <>
      <span className={`${cls} tl`} aria-hidden="true" />
      <span className={`${cls} tr`} aria-hidden="true" />
      <span className={`${cls} bl`} aria-hidden="true" />
      <span className={`${cls} br`} aria-hidden="true" />
    </>
  );
}
