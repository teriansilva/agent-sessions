/** The tile's two characters (#1058). A table, because every interesting case here is an input
 *  shape rather than a behaviour, and because the failure mode is silent: a wrong answer renders a
 *  plausible-looking tile and nobody notices. */
import { describe, expect, test } from "vitest";

import { operatorInitials } from "./operatorInitials";

describe("operatorInitials", () => {
  test.each([
    // [username, expected, why]
    ["nightowl", "NI", "one word → its first two characters"],
    ["mb", "MB", "a two-character name is already the answer"],
    ["m", "M", "one character is a tile, not a padded one"],
    ["marcus braun", "MB", "two words → one character each"],
    ["marcus.braun", "MB", "a dot separates as surely as a space"],
    ["marcus-braun", "MB", "so does a dash"],
    ["marcus_braun", "MB", "and an underscore"],
    ["ops-01", "O0", "a digit is a word character; `01` is a word"],
    ["  admin  ", "AD", "surrounding whitespace is not a word"],
    ["Марк", "МА", "non-ASCII letters upcase like any other"],
    ["марк петров", "МП", "…and split on the space like any other"],
    ["", "·", "an empty name is a dot, never an empty box"],
    ["   ", "·", "…and so is whitespace"],
    ["!!", "!!", "a name with no letters still yields its own characters"],
  ])("%s → %s (%s)", (input, expected) => {
    expect(operatorInitials(input)).toBe(expected);
  });

  test("null and undefined are the empty case, not a crash", () => {
    // `username` is `null` on a no-login install and `undefined` before the config lands. Neither
    // is an error, and neither may throw inside a render.
    expect(operatorInitials(null)).toBe("·");
    expect(operatorInitials(undefined)).toBe("·");
  });

  test("astral characters are not split into half a surrogate pair", () => {
    // Each of these is ONE character in two UTF-16 units, so `slice(0, 2)` yields a single
    // character and `[0]` yields half of one — which renders as `�`. Iterating by code point is
    // the whole reason this function spreads rather than slices, and the answer is two CHARACTERS
    // rather than two units.
    expect(operatorInitials("𝒜𝒷lice")).toBe("𝒜𝒷");
    expect([...operatorInitials("𝒜𝒷lice")]).toHaveLength(2);
    expect("𝒜𝒷lice".slice(0, 2)).toBe("𝒜"); // what the naive version would have shown
  });

  test("an emoji is a separator, not an initial", () => {
    // It is neither a letter nor a digit, so it splits like a space — the tile shows the name
    // beside it rather than a picture nobody chose as their identity.
    expect(operatorInitials("🦊fox")).toBe("FO");
    expect(operatorInitials("🦊 red fox")).toBe("RF");
  });

  test("the answer never depends on the reader's locale", () => {
    // `toLocaleUpperCase` under `tr` maps `i` → `İ`, which would render the SAME username
    // differently for two operators. The tile is an identity cue; it must be stable.
    expect(operatorInitials("ivan")).toBe("IV");
    expect(operatorInitials("ivan")).toBe("ivan".slice(0, 2).toUpperCase());
  });
});
