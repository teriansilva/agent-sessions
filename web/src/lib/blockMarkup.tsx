import type { ReactNode } from "react";
import { inlineMarkup } from "./inlineMarkup";

/** Block formatter for AGENT-AUTHORED text (#1168) — the NEEDS YOU details' "last words". Every
 *  engine's final message is markdown, so the structure an operator reads by (bullets, headings,
 *  code) is mapped onto React elements from a deliberately small, ENUMERATED subset:
 *
 *    `- ` / `* ` / `+ ` lines → <ul><li>     `1. ` / `1) ` lines → <ol><li>
 *    `# …` … `###### …`       → <strong> line      ``` fences      → <pre><code>
 *    blank line               → paragraph break    inline          → `inlineMarkup` (bold, code)
 *
 *  Same rule as `inlineMarkup` (#744): there is NO html sink — no `dangerouslySetInnerHTML`, no
 *  markdown library — so a link, an image, raw HTML or anything else outside the subset stays the
 *  literal characters it is. The text is a transcript the app doesn't control; prompt-injected
 *  markup must never become live markup in the app's chrome.
 *
 *  It never throws on a partial message: the server caps last words from the END (`…` + tail), so
 *  the text can open mid-list or inside a fence. An unclosed fence runs to the end as code. */
const BULLET = /^\s*[-*+]\s+(.*)$/;
const ORDERED = /^\s*(\d{1,9})[.)]\s+(.*)$/;
const HEADING = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/;
const FENCE = /^\s*(```|~~~)/;

type Block =
  | { kind: "p"; lines: string[] }
  | { kind: "h"; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; start: number; items: string[] }
  | { kind: "code"; lines: string[] };

function parse(text: string): Block[] {
  const blocks: Block[] = [];
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = FENCE.exec(line);
    if (fence) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trimStart().startsWith(fence[1]))
        code.push(lines[i++]);
      i++; // the closing fence (or past the end when unclosed)
      blocks.push({ kind: "code", lines: code });
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ kind: "h", text: heading[1] });
      i++;
      continue;
    }
    const bullet = BULLET.exec(line);
    const ordered = bullet ? null : ORDERED.exec(line);
    if (bullet || ordered) {
      const items: string[] = [];
      const re = bullet ? BULLET : ORDERED;
      while (i < lines.length && lines[i].trim()) {
        const m = re.exec(lines[i]);
        if (m) items.push(bullet ? m[1] : m[2]);
        else if (/^\s/.test(lines[i]) && items.length && !FENCE.test(lines[i]))
          items[items.length - 1] += `\n${lines[i].trim()}`; // an indented continuation
        else break;
        i++;
      }
      blocks.push(
        bullet
          ? { kind: "ul", items }
          : { kind: "ol", start: Number(ordered![1]), items },
      );
      continue;
    }
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !FENCE.test(lines[i]) &&
      !HEADING.test(lines[i]) &&
      !BULLET.test(lines[i]) &&
      !ORDERED.test(lines[i])
    )
      para.push(lines[i++]);
    blocks.push({ kind: "p", lines: para });
  }
  return blocks;
}

export function blockMarkup(text: string): ReactNode[] {
  return parse(text).map((b, key) => {
    switch (b.kind) {
      case "h":
        return (
          <p key={key}>
            <strong>{inlineMarkup(b.text)}</strong>
          </p>
        );
      case "ul":
        return (
          <ul key={key}>
            {b.items.map((it, j) => (
              <li key={j}>{inlineMarkup(it)}</li>
            ))}
          </ul>
        );
      case "ol":
        return (
          <ol key={key} start={b.start}>
            {b.items.map((it, j) => (
              <li key={j}>{inlineMarkup(it)}</li>
            ))}
          </ol>
        );
      case "code":
        return (
          <pre key={key}>
            <code>{b.lines.join("\n")}</code>
          </pre>
        );
      default:
        return <p key={key}>{inlineMarkup(b.lines.join("\n"))}</p>;
    }
  });
}
