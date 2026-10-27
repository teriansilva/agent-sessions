/** Agent text as a Markdown element TREE (#1332, #1727): the rules `Markdown` renders with, as a
 *  plain function, so a live reply can hand it to `RevealText` as its `markup`. RevealText walks the
 *  tree to fade only the words that are new and put the caret after the last one; a component
 *  element (`<Markdown>`) has no text to walk. The safety rules are the component's: no raw HTML,
 *  links only to `http(s):`/`mailto:` with `rel="noopener noreferrer"`, images never fetched. */
import type { ReactNode } from "react";
import markdownElements, { defaultUrlTransform } from "react-markdown";
import type { Components, Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import { CodeBlock } from "./CodeBlock";
import styles from "./Markdown.module.css";

const REMARK_PLUGINS = [remarkGfm];

const SAFE_SCHEME = /^(https?:|mailto:)/i;

function safeUrl(url: string): string {
  const cleaned = defaultUrlTransform(url);
  return SAFE_SCHEME.test(cleaned) ? cleaned : "";
}

const COMPONENTS: Components = {
  a: ({ href, children }) =>
    href ? (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    ) : (
      <span className={styles.deadLink}>{children}</span>
    ),
  img: ({ alt }) => <span className={styles.image}>[image{alt ? `: ${alt}` : ""}]</span>,
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
  table: ({ children }) => (
    <div className={styles.tableWrap}>
      <table>{children}</table>
    </div>
  ),
};

/** The options `Markdown` renders with. */
export const MARKDOWN_OPTIONS: Readonly<Options> = {
  remarkPlugins: REMARK_PLUGINS,
  urlTransform: safeUrl,
  components: COMPONENTS,
};

// The last few trees, by text: RevealText asks for the previous and the current text's tree on
// every change, and a pane re-renders far more often than a reply changes.
const TREES = new Map<string, ReactNode>();
const TREES_KEPT = 8;

/** `text` parsed exactly as `<Markdown text>` renders it, as elements (inside the same wrapper). */
export function markdownTree(text: string): ReactNode {
  const hit = TREES.get(text);
  if (hit !== undefined) {
    TREES.delete(text);
    TREES.set(text, hit);
    return hit;
  }
  const tree = <div className={styles.md}>{markdownElements({ ...MARKDOWN_OPTIONS, children: text })}</div>;
  TREES.set(text, tree);
  while (TREES.size > TREES_KEPT) TREES.delete(TREES.keys().next().value as string);
  return tree;
}
