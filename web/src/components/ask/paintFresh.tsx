/** The formatted-tree helpers behind RevealText (#1531): what a tree paints, and the same tree with
 *  its fresh tail wrapped to fade in and the caret after its last word. Separate from the component
 *  so fast refresh keeps working (a component file exports components only). */
import {
  Children,
  cloneElement,
  Fragment,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from "react";

/** The text a formatted tree paints, in order: its string leaves, concatenated. */
export function renderedText(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(renderedText).join("");
  if (isValidElement(node)) {
    const props = node.props as { children?: ReactNode };
    return renderedText(props.children);
  }
  return "";
}

export function commonPrefix(x: string, y: string): number {
  let i = 0;
  while (i < x.length && i < y.length && x[i] === y[i]) i++;
  return i;
}

/** The formatted tree with every rendered character at or after `from` wrapped to fade in, and the
 *  caret after the last text leaf when asked. Elements keep their identity and keys. */
export function paintFresh(node: ReactNode, from: number, caret: boolean): ReactNode {
  const total = renderedText(node).length;
  let pos = 0;
  let caretPlaced = !caret;
  const caretEl = (
    <span key="nj-caret" className="nj-caret" aria-hidden="true" data-testid="ask-caret" />
  );
  const walk = (n: ReactNode, key?: string | number): ReactNode => {
    if (n === null || n === undefined || typeof n === "boolean") return n;
    if (typeof n === "string" || typeof n === "number") {
      const s = String(n);
      const start = pos;
      pos += s.length;
      const cut = Math.max(0, Math.min(s.length, from - start));
      const parts: ReactNode[] = [];
      if (cut > 0) parts.push(s.slice(0, cut));
      if (cut < s.length)
        parts.push(
          <span key={`f${start}`} className="nj-word">
            {s.slice(cut)}
          </span>,
        );
      if (!caretPlaced && pos >= total) {
        caretPlaced = true;
        parts.push(caretEl);
      }
      if (parts.length === 1 && typeof parts[0] === "string") return parts[0];
      return <Fragment key={key ?? `t${start}`}>{parts}</Fragment>;
    }
    if (Array.isArray(n)) return Children.toArray(n).map((c, i) => walk(c, i));
    if (isValidElement(n)) {
      const el = n as ReactElement<{ children?: ReactNode }>;
      if (el.props.children === undefined) return el;
      const kids = Children.toArray(el.props.children).map((c, i) => walk(c, i));
      return cloneElement(el, undefined, ...kids);
    }
    return n;
  };
  const out = walk(node);
  return caretPlaced ? out : (
    <>
      {out}
      {caretEl}
    </>
  );
}
