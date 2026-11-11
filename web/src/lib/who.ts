/** Who wrote a message, as a disc (#1931): a stable colour and an initial for a visitor's name.
 *
 *  The colour is a pure function of the name, so the same name gets the same disc on every
 *  reload, in every session, in the operator's pane and the share viewer alike. Names are
 *  self-chosen labels, not identities: two visitors who type the same name share a colour, as they
 *  already share a label. Imported by the share bundle too, so it imports nothing. */

/** The `--who-N` tokens `tokens.css` defines, spelled out so the token guard can see every read. */
const WHO_VARS = [
  "var(--who-1)",
  "var(--who-2)",
  "var(--who-3)",
  "var(--who-4)",
  "var(--who-5)",
  "var(--who-6)",
  "var(--who-7)",
  "var(--who-8)",
] as const;

export const WHO_COLORS = WHO_VARS.length;

function fold(name: string): string {
  return name.trim().normalize("NFC").toLocaleLowerCase("en");
}

/** FNV-1a (32-bit) over the folded name's UTF-16 units. */
function fnv1a(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

/** 1…WHO_COLORS: which `--who-N` the name takes. */
export function whoIndex(name: string): number {
  return (fnv1a(fold(name)) % WHO_COLORS) + 1;
}

export function whoColor(name: string): string {
  return WHO_VARS[whoIndex(name) - 1];
}

/** The first user-perceived character, upper-cased; `?` for an empty name. */
export function whoInitial(name: string | null | undefined): string {
  const s = (name ?? "").trim();
  if (!s) return "?";
  const Seg = (Intl as unknown as { Segmenter?: typeof Intl.Segmenter }).Segmenter;
  const it = Seg
    ? new Seg(undefined, { granularity: "grapheme" }).segment(s)[Symbol.iterator]().next()
    : null;
  const first = it && !it.done ? it.value.segment : Array.from(s)[0];
  return first.toLocaleUpperCase();
}
