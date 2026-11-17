import {
  Children,
  isValidElement,
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import ReactMarkdown, { type Components, type Options } from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { highlightFence, SYNTAX_CSS, type Segment } from "../editor/highlight";
import { MarkdownImage } from "./MarkdownImage";
import { MermaidBlock } from "./MermaidBlock";
import { classifyLink, createSlugger, fenceLanguage, splitFrontMatter } from "./markdownView";
import styles from "./markdownDocument.module.css";

/** A Markdown FILE, rendered (#2114) — the file viewer's Preview. Its own lazy chunk: a session
 *  that never opens a `.md` downloads none of this.
 *
 *  Not the agent-reply renderer (`structured/Markdown.tsx`, #1332): a file is a document someone
 *  keeps in a repo, so it gets what a forge shows — inline HTML, images from the repo, diagrams,
 *  real heading sizes. Its content is still untrusted (agents write these files), so:
 *  - inline HTML is parsed (`rehype-raw`) and then SANITISED (`rehype-sanitize`, its default
 *    GitHub-derived schema): no script, no event handlers, no `style`, no frames, no
 *    `javascript:`/`data:` URLs; ids and names get the `user-content-` prefix;
 *  - images load only from the box, through the files API (`MarkdownImage`);
 *  - diagrams render inside an `<img>` (`MermaidBlock`);
 *  - only `http(s):`/`mailto:` links leave the app, in a new tab with `noopener noreferrer`. */

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
function rehypeHeadingIds() {
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

/** The text of a react-markdown `<code>` child. */
function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement(node)) return textOf((node.props as { children?: ReactNode }).children);
  return "";
}

function FencedCode({ code, lang, label }: { code: string; lang: string | null; label?: string }) {
  const [segments, setSegments] = useState<{ code: string; s: Segment[] } | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let live = true;
    highlightFence(code, lang).then(
      (s) => live && s && setSegments({ code, s }),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [code, lang]);
  const copy = () => {
    void navigator.clipboard?.writeText(code).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      },
      () => {},
    );
  };
  const segs = segments?.code === code ? segments.s : null;
  return (
    <figure className={styles.block} data-md-code={lang ?? "text"}>
      <figcaption className={styles.blockHead}>
        <span>{label ?? lang ?? "text"}</span>
        <button type="button" className={styles.copy} onClick={copy} aria-label="Copy code">
          {copied ? "Copied" : "Copy"}
        </button>
      </figcaption>
      <pre className={styles.blockCode}>
        <code>
          {segs
            ? segs.map((s, i) =>
                s.cls ? (
                  <span key={i} className={s.cls}>
                    {s.text}
                  </span>
                ) : (
                  s.text
                ),
              )
            : code}
        </code>
      </pre>
    </figure>
  );
}

/** Find an anchor target inside the preview: sanitised ids/names carry `user-content-`, a hand
 *  link (`#install`) names it without. */
function findAnchor(root: HTMLElement, id: string): HTMLElement | null {
  for (const candidate of [`user-content-${id}`, id]) {
    const esc = CSS.escape(candidate);
    const el = root.querySelector<HTMLElement>(`[id="${esc}"], [name="${esc}"]`);
    if (el) return el;
  }
  return null;
}

function MarkdownDocumentImpl({
  path,
  text,
  onOpenPath,
}: {
  path: string;
  text: string;
  /** Opens another file in the viewer (it asks about unsaved edits first). Absent → file links
   *  are shown, not followed. */
  onOpenPath?: (path: string) => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const { frontMatter, body } = useMemo(() => splitFrontMatter(text), [text]);

  const scrollTo = useCallback((id: string) => {
    const root = rootRef.current;
    const el = root && findAnchor(root, id);
    el?.scrollIntoView({ block: "start" });
  }, []);

  const options = useMemo<Readonly<Options>>(() => {
    const components: Components = {
      a: ({ href, children, title }) => {
        const target = classifyLink(path, href);
        if (target.kind === "external") {
          return (
            <a href={target.href} title={title} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          );
        }
        if (target.kind === "anchor") {
          return (
            <a
              href={`#${target.id}`}
              title={title}
              onClick={(e: MouseEvent) => {
                e.preventDefault();
                scrollTo(target.id);
              }}
            >
              {children}
            </a>
          );
        }
        if (target.kind === "file" && onOpenPath) {
          const p = target.path;
          return (
            <a
              href={`#${encodeURIComponent(p)}`}
              title={title ?? p}
              data-md-file-link={p}
              onClick={(e: MouseEvent) => {
                e.preventDefault();
                onOpenPath(p);
              }}
            >
              {children}
            </a>
          );
        }
        return (
          <span className={styles.deadLink} title={target.kind === "file" ? target.path : undefined}>
            {children}
          </span>
        );
      },
      img: ({ src, alt, title, width, height }) => (
        <MarkdownImage
          fromFile={path}
          src={typeof src === "string" ? src : undefined}
          alt={alt}
          title={title}
          width={width}
          height={height}
        />
      ),
      pre: ({ children }) => {
        const child = Children.toArray(children).find(isValidElement) as
          | ReactElement<{ className?: string; children?: ReactNode }>
          | undefined;
        const lang = fenceLanguage(child?.props.className);
        const code = textOf(child ? child.props.children : children).replace(/\n$/, "");
        if (lang === "mermaid") return <MermaidBlock source={code} />;
        return <FencedCode code={code} lang={lang} />;
      },
      // `<picture><source srcset>` is allowed by the sanitiser's schema; a source is the one way a
      // raw-HTML image could name a remote URL the `img` component never sees. Dropped, so a
      // picture falls back to its `<img>`.
      source: () => null,
      table: ({ children }) => (
        <div className={styles.tableWrap}>
          <table>{children}</table>
        </div>
      ),
    };
    return {
      remarkPlugins: [remarkGfm],
      // Footnote ids come out unprefixed here so sanitising adds the one `user-content-` prefix.
      remarkRehypeOptions: { clobberPrefix: "" },
      rehypePlugins: [rehypeRaw, [rehypeSanitize, defaultSchema], rehypeHeadingIds],
      components,
    };
  }, [path, onOpenPath, scrollTo]);

  return (
    <div ref={rootRef} className={styles.doc} data-md-preview="">
      <style>{SYNTAX_CSS}</style>
      {frontMatter !== null && <FencedCode code={frontMatter} lang="yaml" label="Front matter" />}
      <ReactMarkdown {...options}>{body}</ReactMarkdown>
    </div>
  );
}

/** Memoised on its props: parsing is the expensive part, and the viewer re-renders on every
 *  keystroke in Source while this stays mounted only when shown. */
const MarkdownDocument = memo(MarkdownDocumentImpl);
export default MarkdownDocument;
