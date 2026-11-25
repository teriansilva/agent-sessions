import { isValidElement, type ReactNode } from "react";
import { createSlugger } from "./markdownView";

/** The parts of the Markdown pipeline the Files panel's preview (`MarkdownDocument`, #2114) and a
 *  hosted Markdown artifact (`ArtifactMarkdown`, #2210) share: one dialect, one set of heading ids
 *  (`RemoteImage.tsx` is their one remote-image placeholder). No network, no app state: the
 *  artifact renderer runs inside an opaque-origin sandbox with none of the app around it. */

type HastNode = {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
};

const HEADING = /^h[1-6]$/;

function hastText(node: HastNode): string {
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(hastText).join("");
}

/** Heading ids, given AFTER sanitising (which would otherwise prefix them a second time) and with
 *  the same `user-content-` prefix sanitising gives every other id, so `#anchor` lookups need one
 *  rule. A fresh slugger per document keeps the ids stable across renders. */
export function rehypeHeadingIds() {
  return (tree: HastNode) => {
    const slug = createSlugger();
    const walk = (n: HastNode) => {
      if (n.type === "element" && n.tagName && HEADING.test(n.tagName)) {
        n.properties = { ...(n.properties ?? {}), id: `user-content-${slug(hastText(n))}` };
      }
      n.children?.forEach(walk);
    };
    walk(tree);
  };
}

/** Raw HTML in the source, kept as the literal text it is (#2210). Without `rehype-raw` the HTML is
 *  never parsed; this runs BEFORE the sanitiser, which would otherwise drop the node and the words
 *  in it without a trace. */
export function rehypeRawAsText() {
  return (tree: HastNode) => {
    const walk = (n: HastNode) => {
      n.children?.forEach((c, i) => {
        if (c.type === "raw") n.children![i] = { type: "text", value: c.value ?? "" };
        else walk(c);
      });
    };
    walk(tree);
  };
}

/** The text of a react-markdown `<code>` child. */
export function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement(node)) return textOf((node.props as { children?: ReactNode }).children);
  return "";
}

/** Find an anchor target inside a rendered document: sanitised ids/names carry `user-content-`, a
 *  hand link (`#install`) names it without. */
export function findAnchor(root: HTMLElement, id: string): HTMLElement | null {
  for (const candidate of [`user-content-${id}`, id]) {
    const esc = CSS.escape(candidate);
    const el = root.querySelector<HTMLElement>(`[id="${esc}"], [name="${esc}"]`);
    if (el) return el;
  }
  return null;
}
