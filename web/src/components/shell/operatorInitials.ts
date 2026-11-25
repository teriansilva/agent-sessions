/** The two characters on the operator tile (#1058).
 *
 *  A username is not a person's name and cannot be parsed as one — `nightowl`, `admin`, `mb`,
 *  `ops-01`, `Марк` are all legitimate, and none of them has a middle name to drop. So this is
 *  deliberately dumb and total: take up to two letter-or-digit "words", one character each; failing
 *  that, the first two characters of the name; failing that, a single dot, which is a tile rather
 *  than an empty box.
 *
 *  `Intl`-free and locale-free on purpose. `toUpperCase()` without a locale is the right call here:
 *  a locale-aware upcasing would make the SAME username render differently for two operators, and
 *  the tile is an identity cue, not text to read. The Turkish dotless-i case is the classic example
 *  and is exactly what we do NOT want varying.
 *
 *  Code points, not code units: `[...name]` iterates by code point, so an operator whose name starts
 *  with an astral character (an emoji, some CJK extensions) gets that character rather than half of
 *  its surrogate pair — which renders as `�`.
 */
export function operatorInitials(username: string | null | undefined): string {
  const name = (username ?? "").trim();
  if (!name) return "·";
  // Split on anything that is not a letter or a digit — spaces, dots, dashes, underscores.
  const words = name.split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  if (words.length >= 2) {
    return ([...words[0]][0] + [...words[1]][0]).toUpperCase();
  }
  const chars = [...(words[0] ?? name)];
  if (chars.length === 0) return "·";
  return chars.slice(0, 2).join("").toUpperCase();
}
