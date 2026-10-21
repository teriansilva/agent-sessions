/** Version ordering for the What's new gate (#971).
 *
 * PEP 440 orders versions, but a general PEP 440 parser is more than this needs. BattleLab only
 * ever reports a handful of shapes — a release (`0.20.0`), a pre-release (`0.20.0rc1`), a post
 * release, a setuptools-scm development build (`0.20.1.dev2+g1234abc`) and a local tag — so exactly
 * those parse, and everything else is `null`, which the gate reads as "not eligible". The order
 * between them is PEP 440's: `0.20.0.dev1 < 0.20.0a1 < 0.20.0rc1 < 0.20.0 < 0.20.0.post1 <
 * 0.20.1.dev2`. A local label (`+g1234abc`) does not change where a version sorts here.
 *
 * `[0-9]`, not `\d`, and no leading zeros, matching the server's `prefs.release_tuple`. */

export interface ParsedVersion {
  release: readonly [number, number, number];
  /** `[0 | 1 | 2, n]` for `aN` / `bN` / `rcN`. */
  pre: readonly [number, number] | null;
  post: number | null;
  dev: number | null;
}

const N = "(0|[1-9][0-9]{0,3})";
const COUNT = "(0|[1-9][0-9]{0,8})";
const VERSION_RE = new RegExp(
  `^${N}\\.${N}\\.${N}(?:(a|b|rc)${COUNT})?(?:\\.post${COUNT})?(?:\\.dev${COUNT})?(?:\\+[a-z0-9]+(?:\\.[a-z0-9]+)*)?$`,
  "i",
);
const PRE_ORDER: Record<string, number> = { a: 0, b: 1, rc: 2 };

export function parseVersion(value: unknown): ParsedVersion | null {
  if (typeof value !== "string") return null;
  const m = VERSION_RE.exec(value);
  if (!m) return null;
  const num = (s: string | undefined) => (s === undefined ? null : Number(s));
  return {
    release: [Number(m[1]), Number(m[2]), Number(m[3])],
    pre: m[4] === undefined ? null : [PRE_ORDER[m[4].toLowerCase()], Number(m[5])],
    post: num(m[6]),
    dev: num(m[7]),
  };
}

function cmp(a: number, b: number): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function cmpList(a: readonly number[], b: readonly number[]): number {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const c = cmp(a[i] ?? -Infinity, b[i] ?? -Infinity);
    if (c !== 0) return c;
  }
  return 0;
}

/** A development build of a release with no pre/post segment sorts before its pre-releases; a
 *  version with no pre segment sorts after them. */
function preKey(v: ParsedVersion): readonly number[] {
  if (v.pre) return v.pre;
  return v.post === null && v.dev !== null ? [-Infinity] : [Infinity];
}

export function compareVersions(a: ParsedVersion, b: ParsedVersion): number {
  return (
    cmpList(a.release, b.release) ||
    cmpList(preKey(a), preKey(b)) ||
    cmp(a.post ?? -Infinity, b.post ?? -Infinity) ||
    cmp(a.dev ?? Infinity, b.dev ?? Infinity)
  );
}

/** Whether `version` is at or after `target`. Unparseable input and the `0.0.0` fallback an
 *  unbuilt checkout reports are never eligible. */
export function atLeast(version: unknown, target: string): boolean {
  const v = parseVersion(version);
  const t = parseVersion(target);
  if (!v || !t) return false;
  if (v.release.every((n) => n === 0)) return false;
  return compareVersions(v, t) >= 0;
}
