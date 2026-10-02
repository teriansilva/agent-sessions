import { useEffect, useState } from "react";
import { api } from "./api";

/** An upload's image bytes as an object URL, fetched through the app's injectable fetch seam.
 *
 *  Why not `<img src="/api/uploads/…">`: in Home Free app mode the SPA runs on the relay's
 *  origin and private HTTP reaches the connected box only through the tunnel that
 *  `setApiFetch` installs (`homefree/appMount.ts`). A native-origin `src` would ask the relay
 *  for `/api/uploads/<stored>` — a 404 for the thumbnail and the stored name in the relay's
 *  request log (Hermes on #907). Going through `api.uploadBlob` keeps the bytes on the tunnel;
 *  the object URL is revoked when the path changes or the caller unmounts.
 *
 *  State is keyed by the path it was loaded for, so a path change reads as "loading" without
 *  a synchronous reset inside the effect. `enabled` lets the caller defer the fetch until the
 *  image is near the viewport (`UploadImage`) — the gallery must not fan out one full-size
 *  download per card on mount. */
export function useUploadObjectUrl(
  path: string | null,
  enabled = true,
): { src: string | null; failed: boolean } {
  const [state, setState] = useState<{ forPath: string | null; src: string | null; failed: boolean }>(
    { forPath: null, src: null, failed: false },
  );
  useEffect(() => {
    if (!path || !enabled) return;
    let alive = true;
    let url: string | null = null;
    // Cancelled on unmount / path change: a card scrolled away must not keep pulling a 25 MiB
    // original (Hermes on #907, round 2).
    const ctl = new AbortController();
    api
      .uploadBlob(path, ctl.signal)
      .then((blob) => {
        if (!alive) return;
        url = URL.createObjectURL(blob);
        setState({ forPath: path, src: url, failed: false });
      })
      .catch(() => {
        if (alive) setState({ forPath: path, src: null, failed: true });
      });
    return () => {
      alive = false;
      ctl.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [path, enabled]);
  if (state.forPath !== path) return { src: null, failed: false };
  return { src: state.src, failed: state.failed };
}
