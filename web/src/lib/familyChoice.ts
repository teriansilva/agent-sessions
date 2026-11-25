/** The operator's last EXPLICIT client per agent family, on this device (#1728).
 *
 *  The New session picker preselects a family's default client (API first — the server's
 *  `families` decision). When the operator picks another client of that family by hand and starts
 *  it, the pick is remembered here, per device like the rest of the picker's state, and it
 *  overrides the default for that family next time — while that client can still start. Picking the
 *  family's default again forgets it.
 *
 *  This is operator data, not a roster: it is written only from a pick the operator made, holds
 *  ids the roster served, and a stale entry is IGNORED by `pickInFamily` (with its reason), never
 *  trusted. Versioned, bounded and parse-guarded like the runtime hints: a corrupt, hostile or
 *  oversized value is ignored as a whole. */

export const FAMILY_CHOICE_KEY = "battlelab.familyChoice.v1";
const MAX_BYTES = 4_096;
const MAX_FAMILIES = 32;
const ID_RE = /^[a-z][a-z0-9-]{1,23}$/;

/** A family → client map with NO prototype: a family named like an `Object.prototype` member
 *  (`constructor`, `toString`) is an ordinary key, never an inherited function. */
export type FamilyChoices = Record<string, string>;

function emptyChoices(): FamilyChoices {
  return Object.create(null) as FamilyChoices;
}

/** The remembered client for `family`, or null — an OWN key only. */
export function rememberedFor(choices: FamilyChoices, family: string): string | null {
  return Object.hasOwn(choices, family) && typeof choices[family] === "string"
    ? choices[family]
    : null;
}

/** Parse a stored map, or `null` when it is anything but a well-formed, bounded v1 map. */
export function parseFamilyChoices(raw: string | null): FamilyChoices | null {
  if (typeof raw !== "string" || raw.length === 0 || raw.length > MAX_BYTES) return null;
  let doc: unknown;
  try {
    doc = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!doc || typeof doc !== "object" || (doc as { v?: unknown }).v !== 1) return null;
  const choices = (doc as { choices?: unknown }).choices;
  if (!choices || typeof choices !== "object" || Array.isArray(choices)) return null;
  const entries = Object.entries(choices as Record<string, unknown>);
  if (entries.length > MAX_FAMILIES) return null;
  const out = emptyChoices();
  for (const [family, id] of entries) {
    if (!ID_RE.test(family) || typeof id !== "string" || !ID_RE.test(id)) return null;
    out[family] = id;
  }
  return out;
}

/** Every remembered choice on this device (`{}` when none, or storage is unavailable). */
export function readFamilyChoices(): FamilyChoices {
  try {
    return (
      parseFamilyChoices(globalThis.localStorage?.getItem(FAMILY_CHOICE_KEY) ?? null) ??
      emptyChoices()
    );
  } catch {
    return emptyChoices();
  }
}

/** Remember `id` as the explicit choice for `family`, or forget the family's choice (`null`).
 *  Best-effort: a full or unavailable storage only costs the next visit its memory. */
export function rememberFamilyChoice(family: string, id: string | null): void {
  if (!ID_RE.test(family) || (id !== null && !ID_RE.test(id))) return;
  const next = Object.assign(emptyChoices(), readFamilyChoices());
  if (id === null) delete next[family];
  else next[family] = id;
  const kept = Object.entries(next).slice(-MAX_FAMILIES);
  try {
    const raw = JSON.stringify({ v: 1, choices: Object.fromEntries(kept) });
    if (raw.length <= MAX_BYTES) globalThis.localStorage?.setItem(FAMILY_CHOICE_KEY, raw);
  } catch {
    /* quota / private mode: no memory, the default applies */
  }
}
