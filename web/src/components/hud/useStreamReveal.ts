/** Where the newest arrival starts in `text` (#1531, docs/design.md v2 §8 rule 2): the length of
 *  the text before it last changed. Held in state and adjusted during render (React's pattern for
 *  state derived from a prop), so an unrelated re-render never moves the boundary and never
 *  re-fades a word. The first text a component sees is not an arrival (it shows plain), and a text
 *  that does not extend the previous one (a replacement) has no fresh part. */
import { useState } from "react";

export function useStreamReveal(text: string): number {
  const [seen, setSeen] = useState({ text, boundary: text.length });
  if (seen.text !== text) {
    const boundary = text.startsWith(seen.text) ? seen.text.length : text.length;
    setSeen({ text, boundary });
    return boundary;
  }
  return seen.boundary;
}
