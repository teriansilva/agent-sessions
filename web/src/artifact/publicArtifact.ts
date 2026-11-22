/** The public artifact viewer's whole network surface (#2068, #2217): one cookie-less read of a
 *  link's metadata, and, for a password link, one unlock. The artifact itself is never fetched by
 *  this page — it is framed, sandboxed. */

export type ArtifactMeta = {
  title: string;
  kind: "file" | "site";
  updated_at: number;
  /** False: the link was shared without the BattleLab header — the artifact fills the page. */
  header: boolean;
};

/** A public artifact id is 128 bits, 22 url-safe base64 characters (artifact_store.PUBLIC_ID_RE). */
const PATH_RE = /^\/artifacts\/p\/([A-Za-z0-9_-]{22})\/?$/;

/** The id this page was opened for, or null when the address isn't a public artifact link. */
export function publicIdFromPath(pathname: string): string | null {
  return PATH_RE.exec(pathname)?.[1] ?? null;
}

/** The operator's own view (#2270): `/artifacts/o/<view token>/`, where a signed-in Open lands. The
 *  token is the server's signed view token (URL-safe base64 plus dots), never a cookie. */
const OWN_RE = /^\/artifacts\/o\/([A-Za-z0-9_.-]+)\/?$/;

/** The view token this page was opened with, or null when it isn't the operator's own view. */
export function ownTokenFromPath(pathname: string): string | null {
  return OWN_RE.exec(pathname)?.[1] ?? null;
}

/** The operator's own view frames the artifact under the same view token. */
export function ownContentUrl(token: string, search = "", hash = ""): string {
  return `/artifacts/v/${token}/${search}${hash}`;
}

/** The operator's own view's one read: the artifact's title, kind and time, authorised by the
 *  view token in the path (no cookie, like the public read). It always wears the header. */
export async function fetchOwnMeta(
  token: string,
  signal?: AbortSignal,
): Promise<ArtifactMeta> {
  const r = await fetch(`/artifacts/o/${token}/~meta`, { ...OPTS, signal });
  if (r.status === 404) throw new GoneError("expired");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return { ...meta((await r.json()) as Partial<ArtifactMeta>), header: true };
}

/** Where the artifact's own files live: under `~/`, so its relative links stay beneath it. A
 *  password link's live under `~u/<unlock token>/` instead: the sandboxed frame sends no cookie,
 *  so the token rides in the path. */
export function contentUrl(
  id: string,
  search = "",
  hash = "",
  token?: string,
): string {
  const prefix = token ? `~u/${encodeURIComponent(token)}/` : "~/";
  return `/artifacts/p/${id}/${prefix}${search}${hash}`;
}

/** The link no longer works: revoked, expired, unknown, or public links are off on this box. */
export class GoneError extends Error {}

/** The link needs a password. Before it, the server says only whether the page wears the header. */
export class LockedError extends Error {
  readonly header: boolean;
  constructor(header: boolean) {
    super("password required");
    this.header = header;
  }
}

/** A wrong password (`triesLeft` before the pause), or too many of them (`paused`). */
export class UnlockError extends Error {
  readonly triesLeft: number | null;
  readonly paused: boolean;
  constructor(triesLeft: number | null, paused = false) {
    super(paused ? "paused" : "wrong password");
    this.triesLeft = triesLeft;
    this.paused = paused;
  }
}

const OPTS: RequestInit = {
  credentials: "omit",
  cache: "no-store",
  referrerPolicy: "no-referrer",
};

function meta(body: Partial<ArtifactMeta>): ArtifactMeta {
  return {
    title: typeof body.title === "string" ? body.title : "",
    kind: body.kind === "site" ? "site" : "file",
    updated_at: typeof body.updated_at === "number" ? body.updated_at : 0,
    header: body.header !== false,
  };
}

export async function fetchMeta(
  id: string,
  signal?: AbortSignal,
): Promise<ArtifactMeta> {
  const r = await fetch(`/artifacts/p/${id}/~meta`, { ...OPTS, signal });
  if (r.status === 404) throw new GoneError("gone");
  if (r.status === 401) {
    const body = (await r.json().catch(() => ({}))) as { header?: unknown };
    throw new LockedError(body.header !== false);
  }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return meta((await r.json()) as Partial<ArtifactMeta>);
}

/** Trade the password for the link's meta and its unlock token. The token is kept in memory only
 *  (never storage, never the address bar). */
export async function unlock(
  id: string,
  password: string,
  signal?: AbortSignal,
): Promise<{ meta: ArtifactMeta; token: string }> {
  const r = await fetch(`/artifacts/p/${id}/~unlock`, {
    ...OPTS,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
    signal,
  });
  if (r.status === 404) throw new GoneError("gone");
  if (r.status === 429) throw new UnlockError(0, true);
  if (r.status === 401) {
    const body = (await r.json().catch(() => ({}))) as { tries_left?: unknown };
    throw new UnlockError(
      typeof body.tries_left === "number" ? body.tries_left : null,
    );
  }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const body = (await r.json()) as Partial<ArtifactMeta> & { token?: unknown };
  if (typeof body.token !== "string" || !body.token)
    throw new Error("no token");
  return { meta: meta(body), token: body.token };
}
