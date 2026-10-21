/** HQ contacts (#1525): the gallery's ONLY source. Each entry is an original generated General
 *  whose loop lives in `public/hq/<id>.{webm,mp4,jpg}` (provenance and licences in
 *  `public/hq/SOURCES.md`). The ids are the server's too: `hq_contacts.py` maps each to its
 *  registered voice (`hq_persona_<id>`), and a test pins the two lists equal. Adding a contact means
 *  adding its files, one entry here and one there. */

export interface HqContact {
  id: string;
  name: string;
  region: string;
  poster: string;
  webm: string;
  mp4: string;
}

const contact = (id: string, name: string, region: string): HqContact => ({
  id,
  name,
  region,
  poster: `/hq/${id}.jpg`,
  webm: `/hq/${id}.webm`,
  mp4: `/hq/${id}.mp4`,
});

export const CONTACTS: readonly HqContact[] = [
  contact("asia-woman", "General Hana Seo", "Asia"),
  contact("asia-man", "General Kenji Mori", "Asia"),
  contact("europe-woman", "General Claire Vidal", "Europe"),
  contact("europe-man", "General Lukas Brandt", "Europe"),
  contact("russia-woman", "General Irina Volkova", "Russia"),
  contact("russia-man", "General Dmitri Sokolov", "Russia"),
  contact("us-woman", "General Jessica Hale", "US"),
  contact("us-man", "General Jack Harlan", "US"),
  contact("africa-woman", "General Amara Okafor", "Africa"),
  contact("africa-man", "General Kwame Mensah", "Africa"),
  contact("southamerica-woman", "General Valentina Cruz", "South America"),
  contact("southamerica-man", "General Mateo Ríos", "South America"),
  contact("romania-woman", "General Ileana Lupu", "Romania"),
];

/** The stored choice: a contact id, a new contact every day, or no portrait. */
export type HqChoice = string | "random" | "off";

/** The default when nothing (or something unknown) is stored: it shows the gallery off and keeps
 *  HQ fresh (#1525, a recorded decision). */
export const DEFAULT_HQ_CHOICE: HqChoice = "random";

const BY_ID = new Map(CONTACTS.map((c) => [c.id, c]));

export function contactById(id: string | null | undefined): HqContact | null {
  return (id && BY_ID.get(id)) || null;
}

/** A stored value as a choice: a known id, "random" or "off"; anything else is the default, so a
 *  removed contact never leaves the operator with nothing. */
export function coerceHqChoice(value: unknown): HqChoice {
  if (value === "random" || value === "off") return value;
  if (typeof value === "string" && BY_ID.has(value)) return value;
  return DEFAULT_HQ_CHOICE;
}

/** Random's cycle: a FIXED permutation of the contacts. Day `n` shows `PERM[n % 13]`, so every
 *  contact shows exactly once in any 13 consecutive days, and two consecutive days (the cycle
 *  boundary included: `PERM[12]` and `PERM[0]` are different entries) never repeat a contact. A
 *  `hash(date) % 13` can promise neither. The order interleaves regions and women and men. */
export const PERM: readonly string[] = [
  "southamerica-woman",
  "russia-man",
  "asia-woman",
  "us-man",
  "romania-woman",
  "africa-man",
  "europe-woman",
  "asia-man",
  "us-woman",
  "southamerica-man",
  "russia-woman",
  "europe-man",
  "africa-woman",
];

/** Local calendar days since 1970-01-01: the same number all day, on every device in the same
 *  timezone. */
export function localDayNumber(now: Date): number {
  return Math.floor(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) / 86_400_000);
}

/** Random's pick for the local day `now` falls in. */
export function contactForDay(now: Date): HqContact {
  const n = localDayNumber(now);
  return BY_ID.get(PERM[((n % PERM.length) + PERM.length) % PERM.length])!;
}

/** The contact a choice shows on `now`'s day: null for Off. */
export function resolveHqContact(choice: HqChoice, now: Date): HqContact | null {
  if (choice === "off") return null;
  if (choice === "random") return contactForDay(now);
  return contactById(choice) ?? contactForDay(now);
}

/** Milliseconds from `now` to the next local midnight (at least 1). */
export function msToLocalMidnight(now: Date): number {
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  return Math.max(1, next.getTime() - now.getTime());
}
