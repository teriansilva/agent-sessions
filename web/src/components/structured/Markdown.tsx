import { Children, isValidElement, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import styles from "./Markdown.module.css";

/** Agent text as Markdown (#1332): headings, emphasis, lists, tables, links and fenced code.
 *
 * Model output is untrusted, so this renders React elements only:
 * - raw HTML is never parsed (no `rehype-raw`), so `<script>` and friends show as plain text;
 * - a link keeps only an `http(s):` or `mailto:` target, opens in a new tab with
 *   `rel="noopener noreferrer"`, and any other scheme (`javascript:`, `data:`) becomes inert text;
 * - an image is never loaded: an agent-chosen URL fetched by the operator's browser would leak
 *   that the reply was read (and anything encoded in the URL), so it shows as its alt text. */
export function Markdown({ text }: { text: string }) {
  return (
    <div className={styles.md}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={safeUrl} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

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

function codeOf(children: ReactNode): { lang: string | null; text: string } {
  const child = Children.toArray(children).find(isValidElement) as
    | ReactElement<{ className?: string; children?: ReactNode }>
    | undefined;
  const lang = child?.props.className?.match(/language-([\w+#.-]+)/)?.[1] ?? null;
  const raw = child ? child.props.children : children;
  const text = (Array.isArray(raw) ? raw.join("") : String(raw ?? "")).replace(/\n$/, "");
  return { lang, text };
}

function CodeBlock({ children }: { children: ReactNode }) {
  const { lang, text } = codeOf(children);
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(text).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      },
      () => {},
    );
  };
  return (
    <figure className={styles.block} data-testid="md-code">
      <figcaption className={styles.blockHead}>
        <span className={styles.lang}>{lang ?? "text"}</span>
        <button type="button" className={styles.copy} onClick={copy} aria-label="Copy code">
          {copied ? "copied" : "copy"}
        </button>
      </figcaption>
      <pre>
        <code>{text}</code>
      </pre>
    </figure>
  );
}
