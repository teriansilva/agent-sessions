// Typed client for the FastAPI `/api/*` surface. Same-origin; cookie session auth.
// Mutations (later) attach the CSRF token + are origin-checked server-side.
import type {
  AppConfig,
  EnginesResponse,
  Project,
  SessionsPage,
  SessionsQuery,
  SystemInfo,
  TwoFactorEnrollment,
  UpdateInfo,
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

/** POST a CSRF-guarded mutation that returns 204 (no body) — e.g. confirm/disable 2FA. */
async function postVoid(path: string, body?: unknown): Promise<void> {
  const r = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) throw new ApiError(r.status, `POST ${path} → ${r.status}`);
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
  /** Self-update: compare the running version to the channel's latest. */
  updateCheck: () => getJson<UpdateInfo>("/api/update/check"),
  /** Apply the channel's latest (re-runs the installer detached). CSRF-guarded; 202. */
  updateApply: () => postJson<{ status: string }>("/api/update/apply"),
  /** Persist the UI theme server-side (per-user, across devices). CSRF-guarded. */
  setTheme: (theme: string) => postJson<{ theme: string }>("/api/prefs", { theme }),
  /** Persist the brand accent (#rrggbb) server-side, per-user (#211 Phase 2). CSRF-guarded. */
  setAccent: (accent: string) => postJson<{ accent: string }>("/api/prefs", { accent }),
  /** Persist the sidebar view (list|overview) server-side, per-user (#139). CSRF-guarded. */
  setSidebarView: (view: string) =>
    postJson<{ sidebar_view: string }>("/api/prefs", { sidebar_view: view }),
  /** Persist a partial set of UI preferences (e.g. overview lists, #144). CSRF-guarded. */
  setPrefs: (partial: Record<string, unknown>) =>
    postJson<Record<string, unknown>>("/api/prefs", partial),
  /** Optional TOTP 2FA (#116). All CSRF-guarded. */
  enroll2fa: () => postJson<TwoFactorEnrollment>("/api/2fa/enroll"),
  confirm2fa: (code: string) => postVoid("/api/2fa/confirm", { code }),
  /** Disable 2FA — needs a fresh proof (current code OR password). */
  disable2fa: (proof: { code?: string; password?: string }) =>
    postVoid("/api/2fa/disable", proof),
  /** Regenerate recovery codes — same fresh-proof requirement; returns the new set once. */
  regenerate2fa: (proof: { code?: string; password?: string }) =>
    postJson<{ recovery_codes: string[] }>("/api/2fa/recovery-codes", proof),
  upload,
  projects: () => getJson<{ projects: Project[] }>("/api/projects"),
  sessions: (q?: SessionsQuery) => getJson<SessionsPage>(sessionsUrl(q)),
  rename: (id: string, title: string) =>
    postJson<{ id: string; title: string }>(`/api/sessions/${enc(id)}/rename`, { title }),
  archive: (id: string) =>
    postJson<{ id: string; archived: boolean }>(`/api/sessions/${enc(id)}/archive`),
  unarchive: (id: string) =>
    postJson<{ id: string; archived: boolean }>(`/api/sessions/${enc(id)}/unarchive`),
  /** Bulk-archive every non-archived session older than `hours` (#142). CSRF-guarded. */
  archiveOlder: (hours: number) =>
    postJson<{ archived: number; skipped: number }>("/api/sessions/archive-older", { hours }),
  /** Restart a WEDGED session (#331): kill the live agent process so the next attach resumes it
   *  from disk (conversation preserved). `fp`/`tabId` identify this tab against the owner lease;
   *  `force` overrides the owner guard when a *different* active viewer holds the session (else the
   *  call 409s with the holder). CSRF-guarded. */
  restart: (id: string, opts: { fp?: string; tabId?: string; force?: boolean } = {}) =>
    postJson<{ id: string; restarted: boolean; master: string }>(
      `/api/sessions/${enc(id)}/restart`,
      { fp: opts.fp, tab_id: opts.tabId, force: opts.force ?? false },
    ),
  /** Persisted-scrollback cache size, for the Settings cache panel (#206). */
  scrollbackInfo: () => getJson<{ bytes: number; files: number }>("/api/scrollback"),
  /** Clear the persisted-scrollback cache — scope "all" or "archived" (#206). CSRF-guarded. */
  clearScrollback: (scope: "all" | "archived") =>
    postJson<{ scope: string; removed: number; bytes_freed: number }>("/api/scrollback/clear", {
      scope,
    }),
  /** Sign out: clear the session server-side, then hard-navigate to the login page (#141). */
  logout,
};

async function logout(): Promise<void> {
  await postVoid("/logout"); // CSRF POST; the server clears the cookie + 303s to /login
  window.location.assign("/login");
}
