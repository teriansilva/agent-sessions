import lockupDark from "../../brand/logo-horizontal-on-dark.svg";
import lockupLight from "../../brand/logo-horizontal-on-light.svg";
import mark from "../../brand/mark-amber.svg";
import markDark from "../../brand/mark-dark.svg";
import notagDark from "../../brand/logo-horizontal-notag-on-dark.svg";
import notagLight from "../../brand/logo-horizontal-notag-on-light.svg";
import wordDark from "../../brand/wordmark-on-dark.svg";
import wordLight from "../../brand/wordmark-on-light.svg";

import s from "./Brand.module.css";

/** The BattleLab logo from the brand kit (web/src/brand, #1590): the "Chevron Plate" mark and the
 *  horizontal lockup. Bundled SVG files (served from /assets/, which even the public share viewer
 *  may load), never redrawn: the kit's rules forbid recolouring, rounding, outlining or stretching
 *  it, so it is an image with a fixed aspect ratio, and the user's accent never touches it. */

/** The mark alone (min 16 px by the kit's rules). Decorative beside a visible name. The amber
 *  plate is the primary icon; `themed` gives the light theme the kit's dark plate instead. */
export function BrandMark({
  size = 20,
  className = "",
  themed = false,
}: {
  size?: number;
  className?: string;
  themed?: boolean;
}) {
  const px = Math.max(16, size);
  const img = (src: string, cls: string) => (
    <img className={`${s.mark} ${cls} ${className}`} src={src} width={px} height={px} alt="" aria-hidden="true" draggable={false} />
  );
  if (!themed) return img(mark, "");
  return (
    <>
      {img(mark, s.onDark)}
      {img(markDark, s.onLight)}
    </>
  );
}

/** The wordmark alone (BATTLE white + LAB amber on dark, all black on light), outlined: no font.
 *  Its height sets its size; `label` names it when no visible text does. */
export function BrandWordmark({
  height = 16,
  className = "",
  label,
}: {
  height?: number;
  className?: string;
  label?: string;
}) {
  const w = Math.round((height * 284) / 32);
  const a11y = label ? { role: "img", "aria-label": label } : { "aria-hidden": true };
  return (
    <span className={`${s.lockup} ${className}`} {...a11y} data-brand-wordmark>
      <img className={s.onDark} src={wordDark} width={w} height={height} alt="" draggable={false} />
      <img className={s.onLight} src={wordLight} width={w} height={height} alt="" draggable={false} />
    </span>
  );
}

/** The horizontal lockup: mark + wordmark (+ the `// COMMAND & CODE` tagline when `tagline`).
 *  Dark grounds get the on-dark lockup, the light theme the on-light one (the wordmark goes black,
 *  the plate stays amber). The kit's minimum is 120 px wide (200 px with the tagline), so the
 *  height is clamped to keep it. */
export function BrandLockup({
  height = 32,
  tagline = false,
  className = "",
  label = "BattleLab",
}: {
  height?: number;
  tagline?: boolean;
  className?: string;
  label?: string;
}) {
  const h = Math.max(height, tagline ? 35 : 21); // 368 × 64 lockups: 200 / 120 px wide minimum
  const w = Math.round((h * 368) / 64);
  return (
    <span className={`${s.lockup} ${className}`} role="img" aria-label={label} data-brand-lockup>
      <img className={s.onDark} src={tagline ? lockupDark : notagDark} width={w} height={h} alt="" draggable={false} />
      <img className={s.onLight} src={tagline ? lockupLight : notagLight} width={w} height={h} alt="" draggable={false} />
    </span>
  );
}
