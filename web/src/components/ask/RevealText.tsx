/** An answer as it arrives (#1171), on NIGHTJAR's streaming contract (#1531, docs/design.md v2 §8
 *  rule 2): **text is never held back.** The whole answer is in the DOM — formatted, readable,
 *  accessible — the moment it arrives; only the words that are NEW since the last text fade in
 *  (`.nj-word`, `--t-stream`), and while the turn is still working a caret rides the end.
 *
 *  It replaced a reveal that wrote the answer out over 600 ms: presentation that held finished text
 *  back from an operator who had already waited for it.
 *
 *  - A new text that extends what was shown (Stage 2's confirmed answer after Stage 1's) fades only
 *    what is new. Where the new and old FORMATTED texts stop agreeing is the boundary — measured in
 *    the rendered text, so markdown structure is never split and a list never re-flows.
 *  - An `instant` text — an answer read back from a kept conversation (#1443) — is history: plain,
 *    no fade, so twelve read-back answers never animate under a scroll that already landed.
 *  - Outside the Full motion mode (Reduced, Off) nothing fades; the text is identical.
 *  - `data-revealing="true"` marks the short window in which fresh words are fading, so a test can
 *    tell a live arrival from history; it clears once the fade is over.
 *
 *  `markup` formats the text (Ask passes `blockMarkup`: lists, bold and code read as such). The fade
 *  is applied by walking the formatted element tree and wrapping the fresh tail of its text leaves,
 *  so formatting and fade never fight. */
import { type ReactNode, useEffect, useState } from "react";

import { useMotion } from "../../theme/motionStore";
import { commonPrefix, paintFresh, renderedText } from "./paintFresh";

/** How long fresh words are marked as fading (the `--t-stream` fade, plus a frame of slack). */
const FRESH_MS = 300;

export function RevealText({
  text,
  className,
  testId,
  instant = false,
  streaming = false,
  markup = (t) => t,
}: {
  text: string;
  className?: string;
  testId?: string;
  /** History: shown plain, never faded. */
  instant?: boolean;
  /** The turn is still working (a Stage 1 answer awaiting Stage 2): the caret rides the end. */
  streaming?: boolean;
  markup?: (text: string) => ReactNode;
}) {
  const { mode } = useMotion();
  const fade = !instant && mode === "full";
  // The formatted text last shown, and where the words that are new since then start (in the
  // RENDERED text). Adjusted during render — React's pattern for state derived from a prop — so a
  // new text is painted, whole, on the very render it arrives in.
  const [seen, setSeen] = useState(() => ({
    text,
    from: fade ? 0 : Number.POSITIVE_INFINITY,
  }));
  let from = seen.from;
  if (seen.text !== text) {
    const before = renderedText(markup(seen.text));
    const after = renderedText(markup(text));
    from = fade ? commonPrefix(before, after) : Number.POSITIVE_INFINITY;
    setSeen({ text, from });
  } else if (!fade && seen.from !== Number.POSITIVE_INFINITY) {
    // A switch away from Full mid-fade settles at once.
    from = Number.POSITIVE_INFINITY;
    setSeen({ text, from });
  }

  const formatted = markup(text);
  const total = renderedText(formatted).length;
  const fresh = from < total;
  // The fade window closes after the fade itself; the text never changes when it does.
  useEffect(() => {
    if (!fresh) return;
    const t = setTimeout(
      () => setSeen((s) => (s.text === text ? { ...s, from: Number.POSITIVE_INFINITY } : s)),
      FRESH_MS,
    );
    return () => clearTimeout(t);
  }, [fresh, text, from]);

  // A still caret in Reduced / Off still says "more is coming"; only Full blinks it (motion.css).
  const caret = streaming;
  return (
    <div
      className={className}
      data-testid={testId}
      data-revealing={fresh ? "true" : undefined}
      data-streaming={streaming ? "true" : undefined}
    >
      {fresh || caret ? paintFresh(formatted, fresh ? from : total, caret) : formatted}
    </div>
  );
}
