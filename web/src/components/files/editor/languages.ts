import type { Extension } from "@codemirror/state";

/** Syntax support by file extension (#950) — ONE map, each language its own lazy chunk.
 *
 *  Adding a language is one entry here and one dependency. An extension not listed renders as
 *  plain text: still editable, still searchable, just uncoloured — never an error. */
type Loader = () => Promise<Extension>;

interface Language {
  label: string;
  load: Loader;
}

const javascript = (label: string, opts: { jsx?: boolean; typescript?: boolean }): Language => ({
  label,
  load: () => import("@codemirror/lang-javascript").then((m) => m.javascript(opts)),
});
const python: Language = {
  label: "Python",
  load: () => import("@codemirror/lang-python").then((m) => m.python()),
};
const json: Language = {
  label: "JSON",
  load: () => import("@codemirror/lang-json").then((m) => m.json()),
};
const css: Language = {
  label: "CSS",
  load: () => import("@codemirror/lang-css").then((m) => m.css()),
};
const html: Language = {
  label: "HTML",
  load: () => import("@codemirror/lang-html").then((m) => m.html()),
};
const markdown: Language = {
  label: "Markdown",
  load: () => import("@codemirror/lang-markdown").then((m) => m.markdown()),
};
const yaml: Language = {
  label: "YAML",
  load: () => import("@codemirror/lang-yaml").then((m) => m.yaml()),
};

const BY_EXTENSION: Record<string, Language> = {
  js: javascript("JavaScript", { jsx: true }),
  mjs: javascript("JavaScript", {}),
  cjs: javascript("JavaScript", {}),
  jsx: javascript("JSX", { jsx: true }),
  ts: javascript("TypeScript", { typescript: true }),
  mts: javascript("TypeScript", { typescript: true }),
  cts: javascript("TypeScript", { typescript: true }),
  tsx: javascript("TSX", { typescript: true, jsx: true }),
  py: python,
  pyw: python,
  json,
  jsonc: json,
  css,
  html,
  htm: html,
  md: markdown,
  markdown,
  yml: yaml,
  yaml,
};

function extensionOf(path: string): string {
  const name = path.split("/").pop() ?? path;
  const dot = name.lastIndexOf(".");
  // A leading dot is a hidden file's name, not an extension (".env", ".gitignore").
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

export function languageFor(path: string): Language | null {
  return BY_EXTENSION[extensionOf(path)] ?? null;
}

export function languageLabel(path: string): string {
  return languageFor(path)?.label ?? "Plain text";
}
