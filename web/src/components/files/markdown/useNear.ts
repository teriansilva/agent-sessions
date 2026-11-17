import { useEffect, useRef, useState } from "react";

/** Whether an element has come within `margin` of the viewport, latched once it has (#2114). An
 *  image or a diagram far down a long document starts no download and no render until the reader
 *  gets near it. Where IntersectionObserver does not exist (jsdom) everything counts as near — the
 *  same rule as `UploadImage` (#907). */
export function useNear<T extends Element>(margin = "300px") {
  const ref = useRef<T>(null);
  const [near, setNear] = useState(() => typeof IntersectionObserver === "undefined");
  useEffect(() => {
    if (near) return;
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setNear(true);
          io.disconnect();
        }
      },
      { rootMargin: margin },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [near, margin]);
  return { ref, near };
}
