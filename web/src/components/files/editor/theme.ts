import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { EditorView } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";

/** CodeMirror on BattleLab tokens (#950) — the ONLY place the editor gets a look.
 *
 *  Every value is a `var(--…)`, never a literal: dark/light and a custom accent then follow with no
 *  JavaScript, and there is exactly one theme owner (tokens.css) rather than a second copy of the
 *  palette inside the editor. Every surface CodeMirror draws is restyled here — gutter, active
 *  line, selection, caret, brackets, search matches, the search panel, tooltips, scrollbars and
 *  the focus reticle — because an unstyled default (rounded buttons, a grey gradient, a blue
 *  selection) is exactly the "embedded widget" look this integration exists to avoid.
 *
 *  Design rules applied (docs/design.md): no border-radius, no gradients, mono uppercase chrome,
 *  `--accent` only for interaction and focus (never as a syntax colour), status hues never used
 *  for syntax. Touch sizes live in filePanel.module.css (`.editorHost`), which has the media query.
 */
export const battlelabTheme = EditorView.theme({
  "&": {
    height: "100%",
    color: "var(--text-1)",
    backgroundColor: "var(--surface-3)",
    fontSize: "12px",
  },
  // Focus is a hairline, not a frame: a 2px amber box around a whole document on every click read
  // as an alarm in review screenshots. Inset so it never paints outside the viewer body; the
  // caret and the active line carry the rest of the "you are here" signal.
  "&.cm-focused": {
    outline: "1px solid var(--accent-soft)",
    outlineOffset: "-1px",
  },
  ".cm-scroller": {
    fontFamily: "var(--font-mono)",
    lineHeight: "1.6",
    scrollbarColor: "var(--line-strong) transparent",
    overscrollBehavior: "contain",
  },
  ".cm-content": {
    padding: "8px 0",
    caretColor: "var(--accent)",
  },
  ".cm-line": {
    padding: "0 16px 0 8px",
  },
  ".cm-cursor, .cm-dropCursor": {
    borderLeft: "2px solid var(--accent)",
    marginLeft: "-1px",
  },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection":
    {
      backgroundColor: "color-mix(in srgb, var(--accent) 26%, transparent)",
    },
  ".cm-activeLine": {
    backgroundColor: "color-mix(in srgb, var(--text-1) 5%, transparent)",
  },
  ".cm-gutters": {
    backgroundColor: "var(--surface-3)",
    color: "var(--text-3)",
    border: "none",
    borderRight: "1px solid var(--line)",
  },
  ".cm-lineNumbers .cm-gutterElement": {
    minWidth: "3ch",
    padding: "0 10px 0 14px",
    fontVariantNumeric: "tabular-nums",
  },
  ".cm-activeLineGutter": {
    backgroundColor: "color-mix(in srgb, var(--text-1) 5%, transparent)",
    color: "var(--text-1)",
  },
  ".cm-matchingBracket, &.cm-focused .cm-matchingBracket": {
    color: "inherit",
    backgroundColor: "color-mix(in srgb, var(--accent) 18%, transparent)",
    outline: "1px solid var(--accent-soft)",
  },
  ".cm-nonmatchingBracket, &.cm-focused .cm-nonmatchingBracket": {
    color: "var(--danger-text)",
    backgroundColor: "transparent",
    outline: "1px solid var(--danger-text)",
  },
  ".cm-searchMatch": {
    backgroundColor: "color-mix(in srgb, var(--accent) 16%, transparent)",
    outline: "1px solid var(--accent-soft)",
  },
  ".cm-searchMatch.cm-searchMatch-selected": {
    backgroundColor: "color-mix(in srgb, var(--accent) 38%, transparent)",
  },
  ".cm-selectionMatch": {
    backgroundColor: "color-mix(in srgb, var(--text-1) 10%, transparent)",
  },
  ".cm-specialChar": {
    color: "var(--warn-text)",
  },

  // Panels (search): the same chrome as the viewer's mode bar.
  ".cm-panels": {
    backgroundColor: "var(--bg-1)",
    color: "var(--text-1)",
    fontFamily: "var(--font-mono)",
  },
  ".cm-panels.cm-panels-top": {
    borderBottom: "1px solid var(--line)",
  },
  ".cm-panels.cm-panels-bottom": {
    borderTop: "1px solid var(--line)",
  },
  ".cm-panel.cm-search": {
    position: "relative",
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    gap: "6px",
    padding: "8px 44px 8px 12px",
    fontSize: "11px",
  },
  ".cm-panel.cm-search br": {
    flexBasis: "100%",
    height: "0",
  },
  ".cm-textfield": {
    margin: "0",
    minHeight: "26px",
    padding: "3px 8px",
    color: "var(--text-1)",
    backgroundColor: "var(--panel)",
    border: "1px solid var(--border)",
    borderRadius: "0",
    fontFamily: "var(--font-mono)",
    fontSize: "11.5px",
    outline: "none",
  },
  ".cm-textfield:hover": {
    borderColor: "var(--accent)",
  },
  ".cm-textfield:focus": {
    borderColor: "var(--accent)",
    boxShadow: "0 0 0 2px var(--accent)",
  },
  ".cm-button": {
    margin: "0",
    minHeight: "26px",
    padding: "0 10px",
    color: "var(--text-1)",
    backgroundColor: "var(--bg-2)",
    backgroundImage: "none",
    border: "1px solid var(--line-strong)",
    borderRadius: "0",
    fontFamily: "var(--font-mono)",
    fontSize: "10.5px",
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    cursor: "pointer",
  },
  ".cm-button:hover": {
    borderColor: "var(--accent)",
    color: "var(--accent)",
  },
  ".cm-button:active": {
    backgroundImage: "none",
    backgroundColor: "var(--row-hover)",
  },
  ".cm-button:focus-visible": {
    outline: "2px solid var(--accent)",
    outlineOffset: "1px",
  },
  ".cm-panel.cm-search label": {
    display: "inline-flex",
    alignItems: "center",
    gap: "5px",
    color: "var(--text-2)",
    fontSize: "10.5px",
    letterSpacing: "0.06em",
    textTransform: "uppercase",
  },
  ".cm-panel.cm-search input[type=checkbox]": {
    margin: "0",
    accentColor: "var(--accent)",
  },
  ".cm-panel.cm-search [name=close]": {
    position: "absolute",
    top: "6px",
    right: "6px",
    width: "30px",
    height: "30px",
    padding: "0",
    border: "0",
    backgroundColor: "transparent",
    color: "var(--text-2)",
    fontSize: "18px",
    lineHeight: "1",
    cursor: "pointer",
  },
  ".cm-panel.cm-search [name=close]:hover": {
    color: "var(--text-1)",
    backgroundColor: "var(--row-hover)",
  },
  ".cm-tooltip": {
    color: "var(--text-1)",
    backgroundColor: "var(--bg-1)",
    border: "1px solid var(--line-strong)",
    borderRadius: "0",
  },
});

/** Syntax colours come from the `--syn-*` tokens (tokens.css), each held at WCAG AA on
 *  `--surface-3` in both themes by contrast.test.ts. None is a status hue or the accent. */
export const battlelabHighlight = syntaxHighlighting(
  HighlightStyle.define([
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
  ]),
);
