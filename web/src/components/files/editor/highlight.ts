import { HighlightStyle, type LanguageSupport } from "@codemirror/language";
import { highlightCode } from "@lezer/highlight";
import { languageFor } from "./languages";
import { SYNTAX_RULES } from "./syntax";

/** Static syntax highlighting for the Markdown preview's fenced code (#2114) — the editor's own
 *  languages (`languages.ts`) and colours (`syntax.ts`), so the preview carries no second
 *  highlighter. Lives in the editor seam because it imports CodeMirror/Lezer (importSeam.test.ts). */

const STYLE = HighlightStyle.define([...SYNTAX_RULES]);

/** The CSS for the classes `highlightFence` hands out. CodeMirror mounts it for a live editor; a
 *  preview renders it itself (one `<style>`), so a fence is coloured with no editor on the page. */
export const SYNTAX_CSS: string = STYLE.module?.getRules() ?? "";

/** Fence names → the file extension `languages.ts` knows them by. */
const ALIASES: Record<string, string> = {
  javascript: "js",
  node: "js",
  typescript: "ts",
  python: "py",
  py3: "py",
  yml: "yaml",
  markdown: "md",
  htm: "html",
  xml: "html",
  svg: "html",
};

export interface Segment {
  text: string;
  /** Space-separated highlight classes; empty for plain text. */
  cls: string;
}

/** `code` split into coloured segments, or `null` when the fence names no language the editor
 *  has (it then renders as plain text — never an error). Line breaks are `"\n"` segments. */
export async function highlightFence(code: string, lang: string | null): Promise<Segment[] | null> {
  if (!lang) return null;
  const key = lang.toLowerCase();
  const language = languageFor(`x.${ALIASES[key] ?? key}`);
  if (!language) return null;
  const support = (await language.load()) as LanguageSupport;
  const parser = support?.language?.parser;
  if (!parser) return null;
  const out: Segment[] = [];
  highlightCode(
    code,
    parser.parse(code),
    STYLE,
    (text, cls) => out.push({ text, cls }),
    () => out.push({ text: "\n", cls: "" }),
  );
  return out;
}
