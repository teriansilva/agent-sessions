import type { ReactNode } from "react";

/** Inline formatter for MODEL-DERIVED text (#744) — currently the session brief's chronological
 *  recap. It maps a deliberately tiny markdown subset onto React elements:
 *
 *    `**bold**`   → <strong>   (the step's leading action verb)
 *    `` `code` `` → <code>     (file names, commands, identifiers)
 *
 *  Everything else stays a plain string. That is the whole design: there is NO html sink here —
 *  no `dangerouslySetInnerHTML`, no markdown library — so React's escaping still owns the output
 *  and any other markup the model emits (a link, an <img>, a <script>, a heading) renders as the
 *  literal characters it is, exactly as the previous `white-space: pre-wrap` block did.
 *
 *  This matters because the recap is written by a model summarising a TRANSCRIPT — text the model
 *  doesn't control either. Prompt-injected markup in a session's output must never become live
 *  markup in the app's chrome, so the safe subset is enumerated rather than sanitised.
 *
 *  Tokens are single-line by construction (`[^*\n]` / `` [^`\n] ``): an unclosed `**` or backtick
 *  can't run away and swallow the rest of the recap. */
const TOKEN = /(\*\*[^*\n]+\*\*|`[^`\n]+`)/g;

export function inlineMarkup(text: string): ReactNode[] {
  // Walk the actual MATCHES rather than splitting and re-sniffing the pieces: a leftover plain
  // run can legitimately open and close with the same marker (`"**one\ntwo**"` — no match,
  // because the token can't cross the newline), and re-testing it with startsWith/endsWith would
  // wrongly promote it to an element. Only something the pattern matched becomes markup.
  const out: ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  for (const m of text.matchAll(TOKEN)) {
    const at = m.index;
    if (at > cursor) out.push(text.slice(cursor, at));
    const token = m[0];
    out.push(
      token.startsWith("`") ? (
        <code key={key++}>{token.slice(1, -1)}</code>
      ) : (
        <strong key={key++}>{token.slice(2, -2)}</strong>
      ),
    );
    cursor = at + token.length;
  }
  if (cursor < text.length) out.push(text.slice(cursor));
  return out;
}
