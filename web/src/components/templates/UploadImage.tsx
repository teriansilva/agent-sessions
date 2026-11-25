import { useEffect, useRef, useState, type ReactNode } from "react";
import { useUploadObjectUrl } from "../../lib/uploadObjectUrl";

/** `<img>` for an upload path (#905), with the bytes fetched through the tunnel-aware seam
 *  (`lib/uploadObjectUrl.ts`) — and only once the slot is near the viewport. A gallery of 200
 *  cards must not start 200 full-size downloads on mount (each upload may be 25 MiB; Hermes on
 *  #907, round 2): the fetch is gated on an IntersectionObserver with a 200px margin and
 *  aborted when the slot unmounts. Where IntersectionObserver does not exist (jsdom) the slot
 *  counts as visible. Renders nothing until the bytes arrive; on failure renders `fallback` —
 *  a missing upload is a 404 from the read-back, the template itself is fine. */
export function UploadImage({
  path,
  alt,
  className,
  fallback = null,
}: {
  path: string;
  alt: string;
  className?: string;
  fallback?: ReactNode;
}) {
  const slotRef = useRef<HTMLSpanElement>(null);
  const [near, setNear] = useState(() => typeof IntersectionObserver === "undefined");
  useEffect(() => {
    if (near) return;
    const el = slotRef.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setNear(true);
          io.disconnect();
        }
      },
      { rootMargin: "200px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [near]);
  const { src, failed } = useUploadObjectUrl(path, near);
  return (
    <span
      ref={slotRef}
      data-upload-slot={path}
      style={{ display: "block", width: "100%", height: "100%" }}
    >
      {failed ? fallback : src ? <img src={src} alt={alt} className={className} data-upload-path={path} /> : null}
    </span>
  );
}
