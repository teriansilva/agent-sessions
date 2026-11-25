/** Streamed text (#1531, docs/design.md v2 §8 rule 2): **never held back.** Every character is in
 *  the DOM the moment it arrives; only the newest words fade in (`.nj-word`, `--t-stream`) and a
 *  caret rides the end while the stream is open. Nothing above the stream moves.
 *
 *  `StreamText` splits the text into what was already shown (plain) and what arrived since the
 *  last render (wrapped per word, so each fades in once). The split point only advances, so a word
 *  never fades twice; a text that is not an extension of the previous one (a replacement) shows
 *  plain at once. Reduced / Off: the fade is off by motion.css; the text is identical. */
import { useStreamReveal } from "./useStreamReveal";

function splitWords(s: string): string[] {
  return s.match(/\s+|\S+/g) ?? [];
}

export function StreamText({
  text,
  streaming,
  className = "",
  testId,
}: {
  text: string;
  /** The stream is still open: show the riding caret. */
  streaming: boolean;
  className?: string;
  testId?: string;
}) {
  const from = useStreamReveal(text);
  const old = text.slice(0, from);
  const fresh = text.slice(from);
  return (
    <span className={className} data-testid={testId} data-streaming={streaming ? "true" : "false"}>
      {old}
      {splitWords(fresh).map((w, i) =>
        /^\s+$/.test(w) ? (
          w
        ) : (
          <span key={`${from}-${i}`} className="nj-word">
            {w}
          </span>
        ),
      )}
      {streaming && <span className="nj-caret" aria-hidden="true" />}
    </span>
  );
}
