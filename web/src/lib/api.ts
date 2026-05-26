// Typed client for the FastAPI `/api/*` surface. Same-origin; cookie session auth.
// Mutations (later) attach the CSRF token + are origin-checked server-side.
import type {
  AppConfig,
  EnginesResponse,
  Project,
  SessionsPage,
  SessionsQuery,
  SystemInfo,
} from "../types/api";

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Where to send an unauthenticated user: the server login form, carrying the
 *  current location so it can bounce back after sign-in (server open-redirect guards). */
export function loginRedirectUrl(loc: { pathname: string; search: string } = location): string {
  return `/login?next=${encodeURIComponent(loc.pathname + loc.search)}`;
}

// One-shot guard: several /api calls can 401 at once (config + sessions on load); we
// only want a single navigation.
let redirecting = false;
function gotoLogin(): void {
  if (redirecting) return;
  redirecting = true;
  location.assign(loginRedirectUrl());
}

/** First-run forced password change → the server-rendered /change-password (the SPA has
 *  no change screen; this route is on the SW navigateFallbackDenylist). */
export function gotoChangePassword(): void {
  if (redirecting) return;
  redirecting = true;
  location.assign("/change-password");
}

/** Handle a 401/403 on an /api call (always throws). 401 = not signed in → /login.
 *  403 = bad CSRF/origin (surfaced) UNLESS the body says a password change is required,
 *  in which case route to /change-password (belt-and-suspenders alongside the config gate). */
async function authGate(r: Response): Promise<never> {
  if (r.status === 401) {
    gotoLogin();
    throw new ApiError(401, "unauthorized");
  }
  let detail = "";
  try {
    detail = ((await r.json()) as { detail?: string })?.detail ?? "";
  } catch {
    /* non-JSON body */
  }
  if (/password change required/i.test(detail)) {
    gotoChangePassword();
    throw new ApiError(403, "password change required");
  }
  throw new ApiError(403, "forbidden");
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) throw new ApiError(r.status, `GET ${path} → ${r.status}`);
  return (await r.json()) as T;
}

// CSRF token for mutations, fetched once via /api/config and cached. The browser
// adds the Origin header on same-origin POSTs; the server checks both.
let csrfToken = "";
export function setCsrfToken(token: string): void {
  csrfToken = token;
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) throw new ApiError(r.status, `POST ${path} → ${r.status}`);
  return (await r.json()) as T;
}

export function sessionsUrl(q: SessionsQuery = {}): string {
  const p = new URLSearchParams();
  p.set("limit", String(q.limit ?? 20));
  p.set("offset", String(q.offset ?? 0));
  p.set("archived", q.archived ? "1" : "0");
  if (q.q?.trim()) p.set("q", q.q.trim());
  if (q.project) p.set("project", q.project);
  if (q.engine) p.set("engine", q.engine);
  return `/api/sessions?${p.toString()}`;
}

const enc = encodeURIComponent;

/** Upload a file (image/context) → server saves it under ~/.agent-sessions/uploads/
 *  and returns a path the agent can read. Multipart, CSRF-guarded (not JSON). */
async function upload(file: File): Promise<{ path: string; name: string }> {
  const fd = new FormData();
  fd.append("file", file, file.name || "pasted");
  const r = await fetch("/api/upload", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrfToken },
    body: fd,
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) throw new ApiError(r.status, `upload → ${r.status}`);
  return (await r.json()) as { path: string; name: string };
}

export const api = {
  config: () => getJson<AppConfig>("/api/config"),
  version: () => getJson<{ version: string }>("/api/version"),
  /** Discovery: every known engine provider with presence / new-session / bin path. */
  engines: () => getJson<EnginesResponse>("/api/engines"),
  /** Host/system info for the Settings → System card (fail-soft fields). */
  system: () => getJson<SystemInfo>("/api/system"),
  /** Persist the UI theme server-side (per-user, across devices). CSRF-guarded. */
  setTheme: (theme: string) => postJson<{ theme: string }>("/api/prefs", { theme }),
  upload,
  projects: () => getJson<{ projects: Project[] }>("/api/projects"),
  sessions: (q?: SessionsQuery) => getJson<SessionsPage>(sessionsUrl(q)),
  rename: (id: string, title: string) =>
    postJson<{ id: string; title: string }>(`/api/sessions/${enc(id)}/rename`, { title }),
  archive: (id: string) =>
    postJson<{ id: string; archived: boolean }>(`/api/sessions/${enc(id)}/archive`),
  unarchive: (id: string) =>
    postJson<{ id: string; archived: boolean }>(`/api/sessions/${enc(id)}/unarchive`),
};
