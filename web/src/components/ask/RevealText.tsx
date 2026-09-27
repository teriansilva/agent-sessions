/** An answer that writes itself out (#1171) rather than landing as a block after a long silence.
 *
 *  The server sends each answer whole (the model calls are JSON-mode, not token streams), so this
 *  is presentation: the text is revealed over `REVEAL_MS` of wall-clock time, and a new text — Stage 2's
 *  confirmed answer replacing Stage 1's — is revealed again from where the two stop agreeing.
 *  Under `prefers-reduced-motion`, or where `matchMedia` is missing, the whole text shows at once.
 *
 *  The full text is always in the accessible tree: the partial copy is painted for the eye and
 *  hidden from assistive tech, so a screen reader never reads half a sentence.
 */
import { useEffect, useState } from "react";

/** How long a reveal takes, whatever the frame rate. Counting FRAMES instead froze a reveal
 *  mid-sentence on a loaded machine, where frames arrive seconds apart. */
const REVEAL_MS = 600;

function reducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return true;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function commonPrefix(x: string, y: string): number {
  let i = 0;
  while (i < x.length && i < y.length && x[i] === y[i]) i++;
  return i;
}

export function RevealText({
  text,
  className,
  testId,
}: {
  text: string;
  className?: string;
  testId?: string;
}) {
  const reduce = reducedMotion();
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
        text
      ) : (
        <>
          <span className="sr-only">{text}</span>
          <span aria-hidden="true">{text.slice(0, shown)}</span>
        </>
      )}
    </div>
  );
}
