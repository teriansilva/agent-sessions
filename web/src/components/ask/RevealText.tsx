/** An answer that writes itself out (#1171) rather than landing as a block after a long silence.
 *
 *  The server sends each answer whole (the model calls are JSON-mode, not token streams), so this
 *  is presentation: the text is revealed over `REVEAL_MS` of wall-clock time, and a new text — Stage 2's
 *  confirmed answer replacing Stage 1's — is revealed again from where the two stop agreeing.
 *  Outside the Full motion mode (#1531: Reduced, Off) the whole text shows at once — including
 *  when the mode changes mid-reveal, which flushes what is left —
 *  and so does an `instant` one: an answer read back from a kept conversation (#1443) is history,
 *  and twelve of them writing themselves out at once would grow the thread under a scroll that had
 *  already landed on its newest entry.
 *
 *  The full text is always in the accessible tree: the partial copy is painted for the eye and
 *  hidden from assistive tech, so a screen reader never reads half a sentence.
 *
 *  `markup` formats the text (Ask's answers pass `blockMarkup`, so a model's lists, bold and code
 *  read as such). The partial copy goes through it too, so the structure grows in place rather than
 *  jumping from raw text when the reveal ends; `blockMarkup` is built to take a cut-off text.
 */
import { type ReactNode, useEffect, useState } from "react";

import { useMotion } from "../../theme/motionStore";

/** How long a reveal takes, whatever the frame rate. Counting FRAMES instead froze a reveal
 *  mid-sentence on a loaded machine, where frames arrive seconds apart. */
const REVEAL_MS = 600;


function commonPrefix(x: string, y: string): number {
  let i = 0;
  while (i < x.length && i < y.length && x[i] === y[i]) i++;
  return i;
}

export function RevealText({
  text,
  className,
  testId,
  instant = false,
  markup = (t) => t,
}: {
  text: string;
  className?: string;
  testId?: string;
  instant?: boolean;
  markup?: (text: string) => ReactNode;
}) {
  // The motion MODE (#1531), read live: a switch to Reduced or Off mid-reveal flushes the rest of
  // the answer at once (below), so a stopped reveal can never leave it truncated.
  const { mode } = useMotion();
  const reduce = instant || mode !== "full";
  const [rev, setRev] = useState(() => ({
    text,
    n: reduce ? text.length : 0,
    from: 0,
  }));
  // A new text continues from what the two agree on, adjusted during render (React's pattern for
  // state derived from a prop) rather than in an effect.
  if (rev.text !== text) {
    const from = commonPrefix(rev.text.slice(0, rev.n), text);
    setRev({ text, n: reduce ? text.length : from, from });
  } else if (reduce && rev.n < text.length) {
    setRev({ ...rev, n: text.length });
  }
  const shown = rev.text === text ? rev.n : 0;
  const from = rev.text === text ? rev.from : 0;

  // ONE frame loop per text, anchored to when it started: a frame that advances nothing still
  // schedules the next, and a slow frame catches up rather than stretching the reveal out.
  useEffect(() => {
    if (reduce) return;
    const at = performance.now();
    let raf = 0;
    const loop = () => {
      const t = Math.min(1, (performance.now() - at) / REVEAL_MS);
      const n = Math.round(from + (text.length - from) * t);
      setRev((r) =>
        r.text === text && n > r.n ? { ...r, n } : r,
      );
      if (t < 1) raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [text, from, reduce]);

  const done = shown >= text.length;
  return (
    <div
      className={className}
      data-testid={testId}
      data-revealing={done ? undefined : "true"}
    >
      {done ? (
        markup(text)
      ) : (
        <>
          <div className="sr-only">{markup(text)}</div>
          <div aria-hidden="true">{markup(text.slice(0, shown))}</div>
        </>
      )}
    </div>
  );
}
