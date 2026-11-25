/** What HQ's conversation switcher shows (`AskSwitcher`): the kept conversations filtered by the
 *  search, the ones with an unseen update pinned on top, the rest under day headings, latest first.
 *  Pure, so the grouping and the matching are pinned without a DOM. */
import type { AskConversationSummary } from "../../types/api";

export type SwitcherGroup = "updates" | "today" | "yesterday" | "week" | "older";

export const GROUP_LABEL: Record<SwitcherGroup, string> = {
  updates: "Updates",
  today: "Today",
  yesterday: "Yesterday",
  week: "Last 7 days",
  older: "Older",
};

export interface SwitcherSection {
  group: SwitcherGroup;
  rows: AskConversationSummary[];
}

/** Lower-case, accents folded: "Ríos" matches "rios". */
function fold(s: string): string {
  return s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

/** Every word of the query appears somewhere in the title, in any order. */
export function titleMatches(title: string, query: string): boolean {
  const words = fold(query).split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const t = fold(title);
  return words.every((w) => t.includes(w));
}

/** Local midnight `back` calendar days before `d` — by the calendar, so a DST change in between
 *  never moves a boundary by an hour. */
function midnight(d: Date, back = 0): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() - back).getTime();
}

/** The day heading a conversation last updated at `ts` (seconds) sits under: today, yesterday,
 *  the five days before that (seven days in all), or older. */
export function dayGroup(ts: number, now: Date): Exclude<SwitcherGroup, "updates"> {
  const t = ts * 1000;
  if (t >= midnight(now)) return "today";
  if (t >= midnight(now, 1)) return "yesterday";
  if (t >= midnight(now, 6)) return "week";
  return "older";
}

/** The row's time, short, since its heading already says the day: "09:22", "Tue 17:47",
 *  "5 Nov". An update row has no day heading, so a day is added when it is not today. */
export function stamp(ts: number, group: SwitcherGroup, now: Date): string {
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const day = group === "updates" ? dayGroup(ts, now) : group;
  if (day === "today") return time;
  if (day === "yesterday") return group === "updates" ? `Yesterday ${time}` : time;
  if (day === "week") return `${d.toLocaleDateString([], { weekday: "short" })} ${time}`;
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString(
    [],
    sameYear ? { day: "numeric", month: "short" } : { day: "numeric", month: "short", year: "numeric" },
  );
}

const ORDER: SwitcherGroup[] = ["updates", "today", "yesterday", "week", "older"];

/** The sections to show, in order; empty sections are left out. `rows` stays latest first within
 *  each section (the server's order). */
export function arrange(
  rows: AskConversationSummary[],
  query: string,
  now: Date,
): SwitcherSection[] {
  const by = new Map<SwitcherGroup, AskConversationSummary[]>();
  for (const c of rows) {
    if (!titleMatches(c.title || "", query)) continue;
    const g: SwitcherGroup = c.unread ? "updates" : dayGroup(c.updated_at, now);
    const list = by.get(g);
    if (list) list.push(c);
    else by.set(g, [c]);
  }
  return ORDER.filter((g) => by.has(g)).map((g) => ({ group: g, rows: by.get(g)! }));
}
