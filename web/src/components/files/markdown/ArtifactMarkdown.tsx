import { Children, isValidElement, useMemo, useRef, useState, type MouseEvent, type ReactElement, type ReactNode } from "react";
import ReactMarkdown, { type Components, type Options } from "react-markdown";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { classifyImage, classifyLink, fenceLanguage, splitFrontMatter } from "./markdownView";
import { findAnchor, rehypeHeadingIds, rehypeRawAsText, textOf } from "./markdownPipeline";
import { RemoteImage } from "./RemoteImage";
import styles from "./markdownDocument.module.css";

/** A hosted Markdown artifact, rendered (#2210): what `/artifacts/…/<file>.md` shows when it is
 *  opened as a page. It runs inside the artifact sandbox (an opaque origin, `routes/artifacts.py`)
 *  from its own classic-script bundle (`web/src/artifact-md/`), with none of the app around it.
 *
 *  The Files panel's dialect (react-markdown + GFM + rehype-sanitize, `markdownPipeline.tsx`), minus
 *  what only a file on the operator's box needs:
 *  - no raw HTML: it is never parsed (no `rehype-raw`) and shows as the literal text it is;
 *  - images: a relative `src` loads from the artifact's own prefix, exactly as written; a remote one
 *    is never fetched (a labelled link instead); `data:`, other schemes and root-absolute paths show
 *    the alt text;
 *  - links: `http(s):`/`mailto:` open a new tab with `noopener noreferrer`; `#id` scrolls; a relative
 *    link stays the relative link it is, inside the artifact; anything else is inert;
 *  - code: no highlighter and no Mermaid, which would ride in every artifact's bundle. */

/** A root-absolute or scheme path would leave the artifact's prefix (sites use relative links). */
function relativeAsWritten(raw: string | undefined | null): string | null {
  const s = (raw ?? "").trim();
  return s && !s.startsWith("/") && !s.startsWith("\\") ? s : null;
}

function copyText(text: string): Promise<void> {
  // The Clipboard API needs `clipboard-write`, which a sandboxed frame is not granted; the legacy
  // command still works on a click inside the frame.
  const legacy = () => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (!ok) throw new Error("copy refused");
  };
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text).catch(legacy);
  return Promise.resolve().then(legacy);
}

function CodeBlock({ code, label }: { code: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    copyText(code).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      },
      () => {},
    );
  };
  return (
    <figure className={styles.block} data-md-code={label}>
      <figcaption className={styles.blockHead}>
        <span>{label}</span>
        <button type="button" className={styles.copy} onClick={copy} aria-label="Copy code">
          {copied ? "Copied" : "Copy"}
        </button>
      </figcaption>
      <pre className={styles.blockCode}>
        <code>{code}</code>
      </pre>
    </figure>
  );
}

export function ArtifactMarkdown({ path, text }: { path: string; text: string }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const { frontMatter, body } = useMemo(() => splitFrontMatter(text), [text]);

  const options = useMemo<Readonly<Options>>(() => {
    const scrollTo = (id: string) => {
      const root = rootRef.current;
      const el = root && findAnchor(root, id);
      el?.scrollIntoView({ block: "start" });
    };
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
        const rel = target.kind === "file" ? relativeAsWritten(href) : null;
        if (rel) {
          return (
            <a href={rel} title={title}>
              {children}
            </a>
          );
        }
        return <span className={styles.deadLink}>{children}</span>;
      },
      img: ({ src, alt, title, width, height }) => {
        const raw = typeof src === "string" ? src : undefined;
        const source = classifyImage(path, raw);
        if (source.kind === "remote") return <RemoteImage href={source.href} host={source.host} alt={alt} />;
        const rel = source.kind === "repo" ? relativeAsWritten(raw) : null;
        if (!rel) {
          const label = alt?.trim();
          return <span className={styles.imageAlt}>[image{label ? `: ${label}` : ""}]</span>;
        }
        return <img src={rel} alt={alt ?? ""} title={title} width={width} height={height} loading="lazy" />;
      },
      pre: ({ children }) => {
        const child = Children.toArray(children).find(isValidElement) as
          | ReactElement<{ className?: string; children?: ReactNode }>
          | undefined;
        const lang = fenceLanguage(child?.props.className);
        const code = textOf(child ? child.props.children : children).replace(/\n$/, "");
        return <CodeBlock code={code} label={lang ?? "text"} />;
      },
      // `<picture><source srcset>` would be the one way to name a remote URL the `img` component
      // never sees. Raw HTML never reaches here, but the rule costs nothing.
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
      rehypePlugins: [rehypeRawAsText, [rehypeSanitize, defaultSchema], rehypeHeadingIds],
      components,
    };
  }, [path]);

  return (
    <div ref={rootRef} className={styles.doc} data-md-rendered="">
      {frontMatter !== null && <CodeBlock code={frontMatter} label="Front matter" />}
      <ReactMarkdown {...options}>{body}</ReactMarkdown>
    </div>
  );
}
