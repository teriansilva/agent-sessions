// Typed client for the FastAPI `/api/*` surface. Same-origin; cookie session auth.
// Mutations (later) attach the CSRF token + are origin-checked server-side.
import type { SessionsPage, SessionsQuery } from "../types/api";

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

export const api = {
  sessions: (q?: SessionsQuery) => getJson<SessionsPage>(sessionsUrl(q)),
};
