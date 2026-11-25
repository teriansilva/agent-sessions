/** The Markdown preview's pure rules (#2114): which files get it, where a relative image or link
 *  points, what a front-matter block is, and the id a heading gets. No React, no DOM — every rule
 *  the preview acts on is decided here and unit-tested here. */

const MARKDOWN_EXTENSIONS = new Set(["md", "markdown", "mdown", "mkd"]);

/** Whether the viewer offers a rendered Preview for this path. `.mdx` is JSX, not Markdown, so it
 *  stays source-only. */
export function isMarkdownPath(path: string): boolean {
  const name = path.split("/").pop() ?? path;
  const dot = name.lastIndexOf(".");
  return dot > 0 && MARKDOWN_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
}

const SCHEME = /^[a-z][a-z0-9+.-]*:/i;

function dirname(path: string): string {
  const i = path.lastIndexOf("/");
  return i <= 0 ? "/" : path.slice(0, i);
}

/** `target` (a Markdown `src`/`href` with no scheme) as an absolute path, from the file that
 *  names it. `..` is collapsed here; one that climbs above `/` is refused (`null`). The server's
 *  own root boundary (`contained_path`) still decides what may be read — this only makes sure the
 *  path sent is the one the document meant. A leading `/` is an absolute path as written. */
export function resolveRelative(fromFile: string, target: string): string | null {
  let t: string;
  try {
    t = decodeURI(target);
  } catch {
    t = target;
  }
  if (!t || SCHEME.test(t) || t.startsWith("//")) return null;
  const base = t.startsWith("/") ? [] : dirname(fromFile).split("/").filter(Boolean);
  const parts = [...base];
  for (const seg of t.split("/")) {
    if (!seg || seg === ".") continue;
    if (seg === "..") {
      if (!parts.length) return null;
      parts.pop();
    } else parts.push(seg);
  }
  return parts.length ? `/${parts.join("/")}` : null;
}

export type LinkTarget =
  | { kind: "external"; href: string }
  | { kind: "anchor"; id: string }
  | { kind: "file"; path: string }
  | { kind: "none" };

/** Where a link in the document goes. Only `http(s):` and `mailto:` leave the app; another file
 *  opens in the viewer; `#id` scrolls the preview; anything else (`javascript:`, `data:`, a path
 *  above `/`) is inert text. */
export function classifyLink(fromFile: string, href: string | undefined | null): LinkTarget {
  const h = (href ?? "").trim();
  if (!h) return { kind: "none" };
  if (/^(https?:|mailto:)/i.test(h)) return { kind: "external", href: h };
  if (h.startsWith("#")) {
    const id = safeDecode(h.slice(1));
    return id ? { kind: "anchor", id } : { kind: "none" };
  }
  if (SCHEME.test(h) || h.startsWith("//")) return { kind: "none" };
  // A link into another file drops its query and fragment: the viewer opens the file, not a spot
  // in it.
  const bare = h.replace(/[?#].*$/, "");
  const path = bare ? resolveRelative(fromFile, bare) : null;
  return path ? { kind: "file", path } : { kind: "none" };
}

export type ImageSource =
  | { kind: "repo"; path: string }
  | { kind: "remote"; href: string; host: string }
  | { kind: "none" };

/** Where an image comes from. A file on the box is loaded through the files API; a remote one is
 *  never fetched (the CSP refuses it, and a file an agent wrote must not turn opening it into a
 *  request to a third party); `data:` and other schemes are not loaded either. */
export function classifyImage(fromFile: string, src: string | undefined | null): ImageSource {
  const s = (src ?? "").trim();
  if (!s) return { kind: "none" };
  if (/^https?:/i.test(s) || s.startsWith("//")) {
    try {
      const u = new URL(s.startsWith("//") ? `https:${s}` : s);
      return { kind: "remote", href: u.href, host: u.host };
    } catch {
      return { kind: "none" };
    }
  }
  if (SCHEME.test(s)) return { kind: "none" };
  const path = resolveRelative(fromFile, s.replace(/[?#].*$/, ""));
  return path ? { kind: "repo", path } : { kind: "none" };
}

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/** A leading YAML front-matter block (`---` … `---`), split off so it shows as code instead of a
 *  rule and a stray heading. */
export function splitFrontMatter(text: string): { frontMatter: string | null; body: string } {
  const m = /^\uFEFF?---[ \t]*\r?\n([\s\S]*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/.exec(text);
  if (!m) return { frontMatter: null, body: text };
  return { frontMatter: m[1], body: text.slice(m[0].length) };
}

/** GitHub-style heading ids: lower-case, punctuation dropped, spaces to hyphens, and a repeat gets
 *  `-1`, `-2`. One slugger per render, so the ids are the same every time a document renders. */
export function createSlugger(): (text: string) => string {
  const seen = new Map<string, number>();
  return (text: string) => {
    const base =
      text
        .toLowerCase()
        .trim()
        .replace(/[^\p{L}\p{N}\p{M}\s_-]/gu, "")
        .replace(/\s/g, "-") || "section";
    const n = seen.get(base);
    seen.set(base, (n ?? -1) + 1);
    return n === undefined ? base : `${base}-${n + 1}`;
  };
}

/** The fence language of a `className` react-markdown gives a `<code>` (`language-ts`). */
export function fenceLanguage(className: string | undefined): string | null {
  const m = /(?:^|\s)language-([\w+#.-]+)/.exec(className ?? "");
  return m ? m[1].toLowerCase() : null;
}
