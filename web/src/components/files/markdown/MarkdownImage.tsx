import { useEffect, useState } from "react";
import { api, ApiError, filesRawUrl, FILE_BLOB_CAP, isApiTunneled } from "../../../lib/api";
import { classifyImage } from "./markdownView";
import { RemoteImage } from "./RemoteImage";
import { useNear } from "./useNear";
import styles from "./markdownDocument.module.css";

/** An image in a previewed Markdown file (#2114).
 *
 *  - A file on the box loads through `/api/files/raw` — the server's root boundary decides what it
 *    may read. On a direct connection that is a plain lazy `<img src>`; over Home Free the bytes
 *    come through the tunnel (`api.fileBlob`, capped at `FILE_BLOB_CAP`) once the image is near
 *    the viewport, as an object URL revoked on unmount — the same split `MediaPreview` makes.
 *  - A remote image is never fetched: the CSP refuses it, and a file an agent wrote must not turn
 *    opening it into a request to a third party. It shows as a placeholder naming the host.
 *  - Anything else (`data:`, other schemes, a path above `/`) shows as its alt text. */
export function MarkdownImage({
  fromFile,
  src,
  alt,
  title,
  width,
  height,
}: {
  fromFile: string;
  src?: string;
  alt?: string;
  title?: string;
  width?: string | number;
  height?: string | number;
}) {
  const source = classifyImage(fromFile, src);
  const label = alt?.trim() || "";
  if (source.kind === "remote") {
    return <RemoteImage href={source.href} host={source.host} alt={label} />;
  }
  if (source.kind === "none") {
    return <span className={styles.imageAlt}>[image{label ? `: ${label}` : ""}]</span>;
  }
  return (
    <RepoImage
      path={source.path}
      alt={label}
      title={title}
      width={width}
      height={height}
    />
  );
}

function RepoImage({
  path,
  alt,
  title,
  width,
  height,
}: {
  path: string;
  alt: string;
  title?: string;
  width?: string | number;
  height?: string | number;
}) {
  const [tunneled] = useState(isApiTunneled);
  const { ref, near } = useNear<HTMLSpanElement>();
  const [blob, setBlob] = useState<{ url: string } | { failed: string } | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!tunneled || !near) return;
    let live = true;
    let url: string | null = null;
    const ctl = new AbortController();
    api
      .fileBlob(path, { signal: ctl.signal })
      .then((b) => {
        if (!live) return;
        url = URL.createObjectURL(b);
        setBlob({ url });
      })
      .catch((e: unknown) => {
        if (!live || (e instanceof DOMException && e.name === "AbortError")) return;
        setBlob({
          failed:
            e instanceof ApiError && e.status === 413
              ? `is over ${Math.round(FILE_BLOB_CAP / (1024 * 1024))} MB through Home Free`
              : "could not be loaded",
        });
      });
    return () => {
      live = false;
      ctl.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [tunneled, near, path]);

  const name = path.split("/").pop() ?? path;
  const missing = (why: string) => (
    <span className={styles.imageAlt} data-md-image-missing={path} title={path}>
      [image{alt ? `: ${alt}` : ""} — {name} {why}]
    </span>
  );

  if (failed) return missing("could not be loaded");
  if (blob && "failed" in blob) return missing(blob.failed);
  const src = tunneled ? (blob && "url" in blob ? blob.url : null) : filesRawUrl(path);
  return (
    <span ref={ref} className={styles.imageSlot}>
      {src ? (
        <img
          src={src}
          alt={alt}
          title={title}
          width={width}
          height={height}
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
          data-md-image={path}
        />
      ) : (
        <span className={styles.imageAlt}>[image{alt ? `: ${alt}` : ""} — loading]</span>
      )}
    </span>
  );
}
