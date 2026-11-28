/** "Newer version available" for MCP servers (#2431): what the cached hint SAYS. Pure — no React,
 *  no fetch — so `updateModel.test.ts` pins every word.
 *
 *  The hint is read from each package's own registry (`mcp_updates.py`), never from the server's
 *  repository, so it names a PUBLISHED release ("3.4.0 published") and never promises what an
 *  update installs: Update from source reads the repository's head, which may lag the registry,
 *  and its Review always names the version it would actually install (`lagWords`). `unknown` is
 *  never worded as "up to date". */
import type { McpUpdateHint } from "../../types/mcp";

const MIN = 60;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

/** `just now`, `5 min ago`, `3 h ago`, `2 d ago` — from epoch SECONDS. */
export function ago(at: number, now: number = Date.now() / 1000): string {
  const s = Math.max(0, now - at);
  if (s < MIN) return "just now";
  if (s < HOUR) return `${Math.floor(s / MIN)} min ago`;
  if (s < DAY) return `${Math.floor(s / HOUR)} h ago`;
  return `${Math.floor(s / DAY)} d ago`;
}

/** The index's and the Version row's chip: the registry's version verbatim (`v3.4.0` for a Go
 *  module, `0.22.0` for PyPI — never a second `v`). `null` unless a newer release is known. */
export function availableChip(u: McpUpdateHint | null | undefined): string | null {
  return u && u.state === "available" && u.latest ? `${u.latest} available` : null;
}

export interface HintWords {
  /** What the last successful answer says, or that there is none (`null` when only the failure is
   *  worth saying: no answer yet, and the last attempt failed). */
  answer: string | null;
  /** The last failed attempt after that answer, in words. */
  failed: string | null;
  /** A read is running (the server's, or the one this page just asked for). */
  checking: boolean;
  /** A newer release is known: the action may be offered. */
  available: boolean;
}

/** The Updates line on Overview and Settings → Source. */
export function hintWords(u: McpUpdateHint, now: number = Date.now() / 1000): HintWords {
  const checked = u.checked_at ? ` · checked ${ago(u.checked_at, now)}` : "";
  let answer: string | null;
  if (u.state === "available" && u.latest) answer = `${u.latest} published${checked}`;
  else if (u.state === "current") answer = `No newer release${checked}`;
  else if (u.error && !u.checked_at) answer = null;
  else if (u.checked_at) answer = "Last checked over 3 days ago";
  else answer = "Not checked yet";
  const failed = u.error
    ? `Couldn’t check (${u.error})${u.tried_at ? ` · ${ago(u.tried_at, now)}` : ""}`
    : null;
  return { answer, failed, checking: u.checking, available: u.state === "available" && !!u.latest };
}

/** What a throttled Check for updates says (`started: false`): the read it reuses, and why. */
export function throttledWords(u: McpUpdateHint | null, now: number = Date.now() / 1000): string {
  const last = Math.max(u?.checked_at ?? 0, u?.tried_at ?? 0);
  const when = last ? `Checked ${ago(last, now)}` : "Checked a moment ago";
  return `${when}. BattleLab reads a registry at most once every 10 minutes.`;
}

/** Where the hint is read from, per source kind (the registry the operator can recognise). */
export function registryHost(kind: string): string {
  return { npm: "registry.npmjs.org", pypi: "pypi.org", go: "proxy.golang.org" }[kind] ?? "";
}

const core = (v: string) => v.replace(/^[vV]/, "");

/** A loose ordering for wording only (never a decision): dotted numbers first, then a version
 *  with a pre-release / local tail before the same one without. `0` when equal or not comparable. */
export function compareVersions(a: string, b: string): number {
  const split = (v: string) => {
    const m = core(v).match(/^([0-9]+(?:\.[0-9]+)*)(.*)$/);
    return m ? { nums: m[1].split(".").map(Number), tail: m[2] } : null;
  };
  const x = split(a);
  const y = split(b);
  if (!x || !y) return 0;
  for (let i = 0; i < Math.max(x.nums.length, y.nums.length); i++) {
    const d = (x.nums[i] ?? 0) - (y.nums[i] ?? 0);
    if (d) return Math.sign(d);
  }
  if (!x.tail === !y.tail) return 0;
  return x.tail ? -1 : 1;
}

/** Update from source's Review when the repository's head is not the hinted release (#2431): the
 *  Review installs `target` (the head's version), the hint names the registry's newest release.
 *  `null` when there is no hint or they agree. `target` is the installed version when the link is
 *  already up to date. */
export function lagWords(
  target: string,
  u: McpUpdateHint | null | undefined,
  upToDate = false,
): string | null {
  if (!u || u.state !== "available" || !u.latest || !target) return null;
  if (core(target) === core(u.latest)) return null;
  const head = `The repository’s head declares ${target}; the registry’s newest release is ${u.latest}.`;
  if (compareVersions(target, u.latest) > 0) return `${head} This Review installs ${target}.`;
  return upToDate
    ? `${head} There is nothing newer to install from the repository until it catches up.`
    : `${head} Install ${target}, or wait until the repository catches up.`;
}
