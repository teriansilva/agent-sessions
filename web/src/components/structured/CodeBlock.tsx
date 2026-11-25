/** A fenced code block in agent Markdown (#1332): its language, a copy button and the code.
 *
 *  A live reply (#1727) hands the code over decorated by RevealText — its newest words wrapped to
 *  fade in, the caret after the last one. The block renders those children as they are; Copy
 *  takes the plain text alone (no caret, no wrappers). */
import { Children, cloneElement, isValidElement, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import styles from "./Markdown.module.css";

/** The text a node paints (the caret paints none). */
function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement(node)) return textOf((node.props as { children?: ReactNode }).children);
  return "";
}

/** `node` without the one newline a fence's code ends with, taken from its LAST text leaf (so a
 *  caret after it sits at the end of the code, not on a line of its own). Returns the node and
 *  whether the last text leaf was reached. */
function trimFinalNewline(node: ReactNode): [ReactNode, boolean] {
  if (node === null || node === undefined || typeof node === "boolean") return [node, false];
  if (typeof node === "string" || typeof node === "number") {
    const s = String(node);
    if (!s) return [s, false];
    return [s.endsWith("\n") ? s.slice(0, -1) : s, true];
  }
  if (Array.isArray(node)) {
    const out = [...node];
    for (let i = out.length - 1; i >= 0; i--) {
      const [trimmed, done] = trimFinalNewline(out[i]);
      out[i] = trimmed;
      if (done) return [out, true];
    }
    return [out, false];
  }
  if (isValidElement(node)) {
    const el = node as ReactElement<{ children?: ReactNode }>;
    if (el.props.children === undefined) return [el, false];
    const kids = Children.toArray(el.props.children);
    const [trimmed, done] = trimFinalNewline(kids);
    return [cloneElement(el, undefined, ...(trimmed as ReactNode[])), done];
  }
  return [node, false];
}

function codeOf(children: ReactNode): { lang: string | null; code: ReactNode; text: string } {
  const child = Children.toArray(children).find(isValidElement) as
    | ReactElement<{ className?: string; children?: ReactNode }>
    | undefined;
  const lang = child?.props.className?.match(/language-([\w+#.-]+)/)?.[1] ?? null;
  const raw = child ? child.props.children : children;
  const text = textOf(raw).replace(/\n$/, "");
  // Plain code (a finished reply) stays the string it always was; decorated code keeps its marks.
  const code = typeof raw === "string" ? text : trimFinalNewline(raw)[0];
  return { lang, code, text };
}

export function CodeBlock({ children }: { children: ReactNode }) {
  const { lang, code, text } = codeOf(children);
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
        <code>{code}</code>
      </pre>
    </figure>
  );
}
