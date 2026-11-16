/** The public artifact viewer's whole network surface (#2068): one cookie-less read of a link's
 *  metadata. The artifact itself is never fetched by this page — it is framed, sandboxed. */

export type ArtifactMeta = { title: string; kind: "file" | "site"; updated_at: number };

/** A public artifact id is 128 bits, 22 url-safe base64 characters (artifact_store.PUBLIC_ID_RE). */
const PATH_RE = /^\/artifacts\/p\/([A-Za-z0-9_-]{22})\/?$/;

/** The id this page was opened for, or null when the address isn't a public artifact link. */
export function publicIdFromPath(pathname: string): string | null {
  return PATH_RE.exec(pathname)?.[1] ?? null;
}

/** Where the artifact's own files live: under `~/`, so its relative links stay beneath it. */
export function contentUrl(id: string, search = "", hash = ""): string {
  return `/artifacts/p/${id}/~/${search}${hash}`;
}

/** The link no longer works: revoked, unknown, or public links are off on this box. */
export class GoneError extends Error {}

export async function fetchMeta(id: string, signal?: AbortSignal): Promise<ArtifactMeta> {
  const r = await fetch(`/artifacts/p/${id}/~meta`, {
    credentials: "omit",
    cache: "no-store",
    referrerPolicy: "no-referrer",
    signal,
  });
  if (r.status === 404) throw new GoneError("gone");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const body = (await r.json()) as Partial<ArtifactMeta>;
  return {
    title: typeof body.title === "string" ? body.title : "",
    kind: body.kind === "site" ? "site" : "file",
    updated_at: typeof body.updated_at === "number" ? body.updated_at : 0,
  };
}
