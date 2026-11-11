/** The operator's avatar as an object URL (#1931), fetched once per version through the app's
 *  fetch seam and shared by every disc that shows it.
 *
 *  Why not `<img src="/api/profile/avatar?v=…">`: in Home Free app mode the SPA runs on the
 *  relay's origin, and only `apiFetch` (the tunnel, `homefree/appMount.ts`) reaches the connected
 *  box — a native-origin `src` asks the relay and gets nothing (the same reason as
 *  `uploadObjectUrl.ts`). A pane shows the disc beside every message, so the bytes are fetched
 *  ONCE per version and the object URL is shared; the previous version's URL is revoked when a
 *  new one is loaded (an `<img>` already decoded keeps its pixels). */
import { useEffect, useState } from "react";

import { api } from "./api";

let current: { version: string; url: Promise<string | null> } | null = null;

function load(version: string): Promise<string | null> {
  if (current?.version === version) return current.url;
  const previous = current;
  const url = api
    .avatarBlob(version)
    .then((blob) => URL.createObjectURL(blob))
    .catch(() => {
      // A failed read is retried by the next mount rather than remembered.
      if (current?.version === version) current = null;
      return null;
    });
  current = { version, url };
  void previous?.url.then((u) => u && URL.revokeObjectURL(u));
  return url;
}

/** The object URL for `version`, or null while it loads, when it failed, or with no avatar. */
export function useOperatorAvatarSrc(version: string | null | undefined): string | null {
  const [state, setState] = useState<{ version: string; src: string | null } | null>(null);
  useEffect(() => {
    if (!version) return;
    let alive = true;
    void load(version).then((src) => {
      if (alive) setState({ version, src });
    });
    return () => {
      alive = false;
    };
  }, [version]);
  return version && state?.version === version ? state.src : null;
}

/** Tests only: forget the shared URL. */
export function resetOperatorAvatarCache(): void {
  current = null;
}
