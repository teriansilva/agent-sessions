import type { KeyboardEvent } from "react";

const NEXT = new Set(["ArrowRight", "ArrowDown"]);
const PREV = new Set(["ArrowLeft", "ArrowUp"]);

/** Arrow-key movement for a `role="radiogroup"` built from `role="radio"` buttons (#1494).
 *  Native radios get this for free; buttons don't. Put it on the GROUP's `onKeyDown`: the key
 *  event bubbles up from the focused radio, and moving to a sibling both focuses and selects it
 *  (a click), as the ARIA radio pattern asks. Disabled radios are skipped. */
export function onRadioGroupKeyDown(e: KeyboardEvent<HTMLElement>): void {
  const forward = NEXT.has(e.key);
  if (!forward && !PREV.has(e.key) && e.key !== "Home" && e.key !== "End") return;
  const radios = Array.from(
    e.currentTarget.querySelectorAll<HTMLElement>('[role="radio"]'),
  ).filter((r) => !(r as HTMLButtonElement).disabled);
  const at = radios.indexOf(e.target as HTMLElement);
  if (at < 0 || radios.length === 0) return;
  const to =
    e.key === "Home"
      ? 0
      : e.key === "End"
        ? radios.length - 1
        : (at + (forward ? 1 : -1) + radios.length) % radios.length;
  e.preventDefault();
  radios[to].focus();
  radios[to].click();
}

/** Roving tabindex for the same groups (#1494): Tab enters a group ONCE, on its checked radio (or
 *  its first, when nothing is checked, e.g. a custom accent), and the arrows move within it.
 *  Without this every swatch is its own Tab stop — 16 of them for the accent alone. */
export function rovingTabIndex(checked: boolean, index: number, anyChecked: boolean): 0 | -1 {
  return checked || (!anyChecked && index === 0) ? 0 : -1;
}
