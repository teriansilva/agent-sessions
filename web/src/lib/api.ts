// Typed client for the FastAPI `/api/*` surface. Same-origin; cookie session auth.
// Mutations (later) attach the CSRF token + are origin-checked server-side.
import type { AppConfig, SessionsPage, SessionsQuery } from "../types/api";

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (r.status === 401 || r.status === 403) throw new ApiError(r.status, "unauthorized");
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
  if (r.status === 401 || r.status === 403) throw new ApiError(r.status, "unauthorized");
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

export const api = {
  config: () => getJson<AppConfig>("/api/config"),
  sessions: (q?: SessionsQuery) => getJson<SessionsPage>(sessionsUrl(q)),
  rename: (id: string, title: string) =>
    postJson<{ id: string; title: string }>(`/api/sessions/${enc(id)}/rename`, { title }),
  archive: (id: string) =>
    postJson<{ id: string; archived: boolean }>(`/api/sessions/${enc(id)}/archive`),
  unarchive: (id: string) =>
    postJson<{ id: string; archived: boolean }>(`/api/sessions/${enc(id)}/unarchive`),
};
