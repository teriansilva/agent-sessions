import type { SessionMatch, SessionSearch } from "../../types/api";
import styles from "./SessionList.module.css";

/** Where a snippet is from, as a short muted label before it. */
const FIELD_LABEL: Record<string, string> = {
  summary: "summary",
  recap: "recap",
  tag: "tag",
  project: "project",
  report: "report",
  handoff: "continued from",
  first: "first message",
  you: "you",
  agent: "agent",
  subagent: "subagent",
};

/** The snippet line under a matched row: its field, the text with every matched word marked,
 *  the subagent it came from, and how many words sit elsewhere. Text only — never HTML. */
export function MatchSnippet({ m }: { m: SessionMatch }) {
  if (m.tier === "title") return null;
  const label = FIELD_LABEL[m.field] ?? m.field;
  if (m.withheld) {
    return (
      <div className={styles.matchSnippet} data-testid="row-match">
        <span className={styles.matchField}>{label}</span>
        <i className={styles.matchWithheld}>
          Matched in the conversation — snippet withheld (it looks like a credential, or is too long to check)
        </i>
      </div>
    );
  }
  if (!m.snippet) return null;
  const parts: { text: string; mark: boolean }[] = [];
  let at = 0;
  for (const [a, b] of m.marks ?? []) {
    if (a < at || b <= a) continue;
    if (a > at) parts.push({ text: m.snippet.slice(at, a), mark: false });
    parts.push({ text: m.snippet.slice(a, b), mark: true });
    at = b;
  }
  if (at < m.snippet.length) parts.push({ text: m.snippet.slice(at), mark: false });
  return (
    <div className={styles.matchSnippet} data-testid="row-match">
      <span className={styles.matchField}>{label}</span>
      {m.child ? <span className={styles.matchChild}>{m.child}: </span> : null}
      {parts.map((p, i) =>
        p.mark ? (
          <mark key={i} className={styles.matchMark}>
            {p.text}
          </mark>
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
      {m.more ? (
        <span className={styles.matchMore}>
          {" "}
          · + {m.more} more {m.more === 1 ? "word" : "words"} elsewhere in this session
        </span>
      ) : null}
    </div>
  );
}

/** One line above the results when the search could not look everywhere (#2373). */
export function SearchNote({ search }: { search: SessionSearch | null | undefined }) {
  if (!search) return null;
  let text = "";
  if (search.degraded === "common") {
    text =
      "Those words are in too many conversations to search: matched in titles and session details only.";
  } else if (search.mode === "titles" && search.indexing) {
    text = `Still indexing conversations (${search.indexing.indexed.toLocaleString()} of ${search.indexing.total.toLocaleString()}): searching titles and session details only.`;
  } else if (search.mode === "titles") {
    text = "Searching titles and session details only: conversations are not indexed yet.";
  } else if (search.indexing) {
    text = `Still indexing conversations (${search.indexing.indexed.toLocaleString()} of ${search.indexing.total.toLocaleString()}): some may not be found yet.`;
  }
  if (search.incomplete) {
    const n = search.incomplete;
    text = [
      text,
      `${n.toLocaleString()} ${n === 1 ? "conversation" : "conversations"} could not be read whole: ${n === 1 ? "it" : "they"} may be missing from these results.`,
    ]
      .filter(Boolean)
      .join(" ");
  }
  if (search.short) {
    text = [text, "Words under 3 letters are matched in titles and session details only."]
      .filter(Boolean)
      .join(" ");
  }
  if (!text) return null;
  return (
    <div className={styles.searchNote} role="status" data-testid="search-note">
      {text}
    </div>
  );
}
