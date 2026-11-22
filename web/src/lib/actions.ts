/** Library → Actions (#2248 P1): the gallery's and the wizard picker's pure rules. One list, from
 *  `GET /api/actions`; the SPA keeps no list of actions of its own. */
import type { ActionEffect, ActionEntry, ActionGallery } from "../types/actions";

export type CategoryFilter = "all" | string;

/** Whether an action matches what was typed: its name, its line, or its category's name. */
export function actionMatches(a: ActionEntry, needle: string, categoryLabel = ""): boolean {
  const n = needle.trim().toLowerCase();
  if (!n) return true;
  return [a.label, a.description, categoryLabel].join(" ").toLowerCase().includes(n);
}

export function categoryLabel(g: ActionGallery | null, id: string): string {
  return g?.categories.find((c) => c.id === id)?.label ?? id;
}

/** The actions a search and a category chip leave, in the gallery's order. */
export function visibleActions(
  g: ActionGallery,
  search: string,
  category: CategoryFilter,
): ActionEntry[] {
  return g.actions.filter(
    (a) =>
      (category === "all" || a.category === category) &&
      actionMatches(a, search, categoryLabel(g, a.category)),
  );
}

/** The chips: All, then every category that HAS an action, each with how many the search leaves.
 *  A category with no action yet is not a chip (P1 ships five actions in two categories); the page
 *  says more come later instead of drawing chips that filter to nothing. */
export function categoryChips(
  g: ActionGallery,
  search: string,
): { value: CategoryFilter; label: string; n: number }[] {
  const matching = g.actions.filter((a) => actionMatches(a, search, categoryLabel(g, a.category)));
  return [
    { value: "all", label: "All", n: matching.length },
    ...g.categories
      .filter((c) => g.actions.some((a) => a.category === c.id))
      .map((c) => ({
        value: c.id,
        label: c.label,
        n: matching.filter((a) => a.category === c.id).length,
      })),
  ];
}

/** The categories that have no action yet, for the "more come later" line. */
export function comingCategories(g: ActionGallery): string[] {
  return g.categories.filter((c) => !g.actions.some((a) => a.category === c.id)).map((c) => c.label);
}

/** An effect's level (its pips, 1–5) and its words. */
export function effectOf(g: ActionGallery | null, id: ActionEffect): { level: number; label: string } {
  const e = g?.effects.find((x) => x.id === id);
  return { level: e?.level ?? 0, label: e?.label ?? id };
}

function capital(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** Whether the wizard may pick this action now: a requirement that is checked and does NOT hold
 *  makes it unpickable (the needs-setup state), with where to set it up. One not checkable here
 *  (`ok: null`) never blocks. */
export function needsSetup(a: ActionEntry): { words: string; setup: string | null } | null {
  const failing = a.requires.find((r) => r.ok === false);
  if (!failing) return null;
  return { words: capital(failing.detail || `needs ${failing.words}`), setup: failing.setup ?? null };
}

/** The card's requirement line: Ready (with what still depends on the wizard's choice), or the
 *  first requirement that does not hold, in its own words. Colour goes with the word (the LED). */
export function requirementLine(a: ActionEntry): {
  tone: "up" | "degraded";
  word: string;
  extra: string;
} {
  const failing = a.requires.find((r) => r.ok === false);
  if (failing) return { tone: "degraded", word: capital(failing.detail || `needs ${failing.words}`), extra: "" };
  const open = a.requires.filter((r) => r.ok === null).map((r) => r.words);
  return { tone: "up", word: "Ready", extra: open.length ? `needs ${open.join(", ")}` : "" };
}

export function usedByWords(n: number | null): string {
  if (n === null) return "use unknown";
  return n ? `used by ${n}` : "not used yet";
}

/** What the wizard offers for a trigger: the actions whose manifest says that trigger starts them,
 *  in the gallery's order. A review loop's own legacy webhook starts only its review loop. */
export function offeredActions(
  actions: ActionEntry[],
  trigger: string,
  legacy: boolean,
  known: readonly string[],
): ActionEntry[] {
  return actions.filter(
    (a) =>
      known.includes(a.id) &&
      (legacy ? a.id === "forge_review_loop" : a.triggers.includes(trigger)),
  );
}

/** What the action gallery's state holds the wizard's What step for, or `null` when it is ready.
 *  The step picks from the gallery, so without a CURRENT answer it waits: Next, the rail past What
 *  and the save. That is a first read still loading, any read that failed (a refresh over a cached
 *  list included), and a refresh still in flight over a cached list: the kept answer may be stale,
 *  and the refresh may yet fail. A cached list may stay on screen meanwhile. */
export function galleryProblem(state: {
  gallery: ActionGallery | null;
  error: string;
  loading: boolean;
}): string | null {
  if (state.error) return "The actions couldn’t be loaded: press Retry";
  if (!state.gallery) return "Loading the actions…";
  if (state.loading) return "Checking the action list…";
  return null;
}
