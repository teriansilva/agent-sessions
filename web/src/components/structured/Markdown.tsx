import { memo } from "react";
import ReactMarkdown from "react-markdown";
import styles from "./Markdown.module.css";
import { MARKDOWN_OPTIONS } from "./markdownTree";

/** Agent text as Markdown (#1332): headings, emphasis, lists, tables, links and fenced code.
 *
 * Model output is untrusted, so this renders React elements only:
 * - raw HTML is never parsed (no `rehype-raw`), so `<script>` and friends show as plain text;
 * - a link keeps only an `http(s):` or `mailto:` target, opens in a new tab with
 *   `rel="noopener noreferrer"`, and any other scheme (`javascript:`, `data:`) becomes inert text;
 * - an image is never loaded: an agent-chosen URL fetched by the operator's browser would leak
 *   that the reply was read (and anything encoded in the URL), so it shows as its alt text.
 *
 * Parsing is the expensive part — react-markdown runs the whole remark pipeline on every render
 * it gets. Memoized on `text`, a reply is parsed when it changes, not whenever its pane
 * re-renders: a structured pane on the map re-rendered every turn's Markdown on every pointer
 * move of a window drag (the operator's trace: 4.9 s of 11.5 s in `remark-parse`). The rules
 * live in `markdownTree.tsx`, shared with a live reply's word fade (#1727). */
export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className={styles.md}>
      <ReactMarkdown {...MARKDOWN_OPTIONS}>{text}</ReactMarkdown>
    </div>
  );
});
