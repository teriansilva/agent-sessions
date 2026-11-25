import type { TagStyle } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";

/** Syntax colours come from the `--syn-*` tokens (tokens.css), each held at WCAG AA on
 *  `--surface-3` in both themes by contrast.test.ts. None is a status hue or the accent.
 *
 *  ONE rule set for the editor (`theme.ts`) and the Markdown preview's code blocks
 *  (`highlight.ts`, #2114), so a fence in a preview is coloured exactly as the file is in Source. */
export const SYNTAX_RULES: readonly TagStyle[] = [
  {
    tag: [
      t.keyword,
      t.controlKeyword,
      t.operatorKeyword,
      t.definitionKeyword,
      t.moduleKeyword,
      t.modifier,
      t.self,
    ],
    color: "var(--syn-keyword)",
  },
  {
    tag: [t.string, t.special(t.string), t.regexp, t.character, t.attributeValue, t.escape],
    color: "var(--syn-string)",
  },
  {
    tag: [t.number, t.bool, t.null, t.atom, t.unit, t.color, t.constant(t.name)],
    color: "var(--syn-number)",
  },
  {
    tag: [t.comment, t.lineComment, t.blockComment, t.docComment],
    color: "var(--syn-comment)",
    fontStyle: "italic",
  },
  {
    tag: [
      t.function(t.variableName),
      t.function(t.propertyName),
      t.function(t.definition(t.variableName)),
      t.macroName,
    ],
    color: "var(--syn-function)",
    fontWeight: "600",
  },
  {
    tag: [t.typeName, t.className, t.namespace, t.tagName, t.definition(t.typeName)],
    color: "var(--syn-type)",
  },
  {
    tag: [t.propertyName, t.attributeName, t.labelName],
    color: "var(--syn-property)",
  },
  {
    tag: [t.operator, t.punctuation, t.bracket, t.separator, t.derefOperator],
    color: "var(--syn-punct)",
  },
  {
    tag: [t.meta, t.processingInstruction, t.annotation],
    color: "var(--syn-comment)",
  },
  { tag: t.heading, color: "var(--text-1)", fontWeight: "700" },
  { tag: t.strong, fontWeight: "700" },
  { tag: t.emphasis, fontStyle: "italic" },
  { tag: t.strikethrough, textDecoration: "line-through" },
  { tag: t.link, color: "var(--syn-type)", textDecoration: "underline" },
  { tag: t.url, color: "var(--syn-string)" },
  { tag: t.invalid, color: "var(--danger-text)" },
];
