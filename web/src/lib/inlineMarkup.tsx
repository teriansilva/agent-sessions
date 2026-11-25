import type { ReactNode } from "react";
import { DOCS_HOME_URL } from "./links";

/** Inline formatter for MODEL-DERIVED text (#744) — the session brief's chronological recap, and
 *  (through `blockMarkup`) the NEEDS YOU last words and Ask's answers. It maps a deliberately tiny
 *  markdown subset onto React elements:
 *
 *    `**bold**`   → <strong>   (the step's leading action verb)
 *    `*italic*`   → <em>       (stress in a sentence — "what I *can't* do")
 *    `` `code` `` → <code>     (file names, commands, identifiers)
 *    `[label](docs)` / a bare docs URL → <a> (HQ cites the manual it read; see below)
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
 *  can't run away and swallow the rest of the recap.
 *
 *  Links are the one exception, and only to BattleLab's own manual: HQ is told to docs_search the
 *  installed manual and "cite the page with its link", and that citation read as a wall of
 *  `([Playbooks](https://docs…))` characters. A link becomes an <a> ONLY when its target is a page
 *  on `DOCS_HOME_URL` (an https URL with no space, quote, angle bracket or paren in it) — a fixed
 *  host the app already links to from its Help menu. Any other target (another host, `javascript:`,
 *  a lookalike such as `docs.battlelabos.com.evil`) still renders as the literal characters, so
 *  injected text can't put a clickable phishing link into the chrome. The label is plain text. */
const DOCS_HOST = new URL(DOCS_HOME_URL).host;
/** A page on the docs site: the home URL, then any run of URL characters that doesn't end in
 *  sentence punctuation. No whitespace, quotes, brackets, parens or backslashes. */
const DOCS_URL =
  DOCS_HOME_URL.replace(/[.*+?^$()|[\]\\{}]/g, "\\$&") +
  String.raw`(?:[^\s()<>"'\\[\]` + "`" + String.raw`]*[^\s()<>"'\\[\]` + "`" + String.raw`.,;:!?*_])?`;
/** `[label](docs URL)` — group 1 the whole token, 2 the label, 3 the URL. */
const LINK = String.raw`(\[([^\]\n]+)\]\((${DOCS_URL})\))`;
/** A bare docs URL — group 4. Trailing sentence punctuation stays outside it. */
const BARE = `(${DOCS_URL})`;
/** Belt and braces on top of the pattern: the parsed URL is https on the docs host itself. */
function docsHref(url: string): string | null {
  try {
    const u = new URL(url);
    return u.protocol === "https:" && u.host === DOCS_HOST ? u.href : null;
  } catch {
    return null;
  }
}
const TOKEN = /(\*\*[^*\n]+\*\*|`[^`\n]+`)/g;
/** `*italic*` is flanked like CommonMark's: the opening star is followed by a non-space and the
 *  closing one preceded by one; the opening star follows no word character, star or `/`, and the
 *  closing one precedes no word character or star — so `2 * 3 * 4`, `a*b*c` and globs like
 *  `src/*.ts … lib/*.ts` stay literal. The flank before the opening star
 *  is CAPTURED (its own group) rather than looked behind: lookbehind is a SyntaxError on older Safari,
 *  and this module loads with the app shell. */
const ITALIC = /(^|[^\w*/])(\*[^\s*](?:[^*\n]*[^\s*])?\*)(?![\w*])/g;
const ANY = new RegExp(`${LINK}|${BARE}|${TOKEN.source}|${ITALIC.source}`, "g");

export function inlineMarkup(text: string): ReactNode[] {
  // Walk the actual MATCHES rather than splitting and re-sniffing the pieces: a leftover plain
  // run can legitimately open and close with the same marker (`"**one\ntwo**"` — no match,
  // because the token can't cross the newline), and re-testing it with startsWith/endsWith would
  // wrongly promote it to an element. Only something the pattern matched becomes markup.
  const out: ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  for (const m of text.matchAll(ANY)) {
    // Groups: 1–3 a markdown docs link (token, label, URL), 4 a bare docs URL, 5 bold/code, 6–7
    // an italic (its flank, its token). An italic match starts at its flank, which stays plain.
    const token = m[1] ?? m[4] ?? m[5] ?? m[7];
    const at = m.index + (m[6] !== undefined ? m[6].length : 0);
    if (at > cursor) out.push(text.slice(cursor, at));
    const isLink = m[1] !== undefined || m[4] !== undefined;
    // A bare URL counts only on its own — after whitespace, an opening bracket/quote or the start —
    // never as the tail of some other URL (`https://evil/?https://docs…`). Checked here, not
    // looked behind: lookbehind is a SyntaxError on older Safari.
    const standalone = m[4] === undefined || at === 0 || /[\s(["'<]/.test(text[at - 1]);
    const href = isLink && standalone ? docsHref(m[1] !== undefined ? m[3] : m[4]) : null;
    out.push(
      isLink ? (
        href ? (
          <a key={key++} href={href} target="_blank" rel="noopener noreferrer">
            {m[1] !== undefined ? m[2] : token}
          </a>
        ) : (
          token
        )
      ) : token.startsWith("`") ? (
        <code key={key++}>{token.slice(1, -1)}</code>
      ) : token.startsWith("**") ? (
        <strong key={key++}>{token.slice(2, -2)}</strong>
      ) : (
        // An italic can hold a code span (`*run `npm ci` first*`); its content has no star, so
        // only `code` can match inside.
        <em key={key++}>{inlineMarkup(token.slice(1, -1))}</em>
      ),
    );
    cursor = at + token.length;
  }
  if (cursor < text.length) out.push(text.slice(cursor));
  return out;
}
