/** The structured (API) create (#1311), shared by New session and the setup wizard's Launch (#1739)
 *  so both make a session the same way: one operation id per create, kept for a retry. */
import { ApiError, api } from "./api";
import { handStart } from "../components/structured/startHandoff";

/** The structured create whose outcome is unknown (#1311), per tab. */
export const PENDING_CREATE = "battlelab.pendingStructuredCreate";
/** `model` (#1313), `bypass` (#1339) and `effort` (#1407) are part of the create's identity: a
 *  retry reuses the id only for the same choices, so a reload can never resend an id with a
 *  different one. */
export type PendingCreate = {
  engine: string;
  cwd: string;
  model?: string;
  effort?: string;
  id: string;
  bypass?: boolean;
  /** Started from a specialist (#1693): another one, or none, is another create. */
  specialist?: { id: string; version: number };
};

export function sameSpecialist(
  a: { id: string; version: number } | undefined,
  b: { id: string; version: number } | undefined,
): boolean {
  return a?.id === b?.id && a?.version === b?.version;
}

export function readPendingCreate(): PendingCreate | null {
  try {
    const v = JSON.parse(sessionStorage.getItem(PENDING_CREATE) ?? "null") as PendingCreate | null;
    return v &&
      typeof v.engine === "string" &&
      typeof v.cwd === "string" &&
      typeof v.id === "string" &&
      (v.bypass === undefined || typeof v.bypass === "boolean") &&
      (v.effort === undefined || typeof v.effort === "string") &&
      (v.specialist === undefined ||
        (typeof v.specialist?.id === "string" && typeof v.specialist?.version === "number"))
      ? v
      : null;
  } catch {
    return null;
  }
}

export function writePendingCreate(v: PendingCreate | null): void {
  try {
    if (v) sessionStorage.setItem(PENDING_CREATE, JSON.stringify(v));
    else sessionStorage.removeItem(PENDING_CREATE);
  } catch {
    /* storage unavailable: the in-flight guard still prevents a same-page double create */
  }
}

/** A late lookup/action must not offer or clear a different attempt's recovery pointer. */
export function isStoredAttempt(attempt: PendingCreate): boolean {
  const current = readPendingCreate();
  return current?.id === attempt.id &&
    current.engine === attempt.engine &&
    current.cwd === attempt.cwd &&
    (current.model ?? "default") === (attempt.model ?? "default") &&
    Boolean(current.bypass) === Boolean(attempt.bypass) &&
    sameSpecialist(current.specialist, attempt.specialist);
}

export interface StructuredCreateRequest {
  engine: string;
  cwd: string;
  /** `default` (or omitted) = the client's own default. */
  model?: string;
  effort?: string;
  /** Skip permission prompts (#1339). The caller passes it only for a client that can skip. */
  bypass: boolean;
  /** `false`: a skip create is NOT started here; the caller starts it with `startCreated` once it
   *  has re-checked that the launch is still wanted (the setup wizard, which can be dismissed while
   *  the create runs — #1739). Unstarted, the session waits in the session list for Start. */
  start?: boolean;
  specialist?: { id: string; version: number };
  /** API first (#1728): whether `engine` is its family's automatic default (the server refuses it
   *  with the new default if that moved) or an explicit choice. Omitted: the create as before. */
  choice?: "automatic" | "explicit";
}

/** Create one API session and return its key. The operation id is the create's identity: a guarded
 *  retry after an unknown outcome reuses it (the server returns the same session), and a different
 *  agent, folder, model, level, specialist or permission mode is a different create. The guarded
 *  attempt is kept in sessionStorage, so a reload after a lost response retries the SAME create
 *  (Hermes on #1315). A skip create keeps NO recovery slot (Hermes 5913) and launches only once its
 *  create has answered (two-phase start, #1339), fired, not awaited (#1505).
 *
 *  Throws the request's error. A refusal (< 500) has already released this attempt's slot; a lost
 *  answer keeps it. */
export async function createStructuredSession(req: StructuredCreateRequest): Promise<string> {
  const { engine, cwd, bypass: skip } = req;
  const model = req.model ?? "default";
  const effort = req.effort ?? "default";
  const prev = readPendingCreate();
  const attempt: PendingCreate =
    !skip &&
    prev &&
    !prev.bypass &&
    prev.engine === engine &&
    prev.cwd === cwd &&
    (prev.model ?? "default") === model &&
    (prev.effort ?? "default") === effort &&
    sameSpecialist(prev.specialist, req.specialist)
      ? prev
      : {
          engine,
          cwd,
          model,
          // Recorded only when chosen, so a create without a level is the one it always was.
          ...(effort !== "default" ? { effort } : {}),
          id: crypto.randomUUID(),
          bypass: skip,
          ...(req.specialist ? { specialist: req.specialist } : {}),
        };
  if (skip) {
    if (prev?.bypass) writePendingCreate(null);
  } else {
    writePendingCreate(attempt);
  }
  const clearAttempt = () => {
    if (isStoredAttempt(attempt)) writePendingCreate(null);
  };
  try {
    const args = [engine, cwd, attempt.id, model, Boolean(attempt.bypass)] as const;
    const { session_key: key } = await (req.choice
      ? api.structuredCreate(...args, attempt.effort, attempt.specialist, req.choice)
      : attempt.specialist
        ? api.structuredCreate(...args, attempt.effort, attempt.specialist)
        : attempt.effort
          ? api.structuredCreate(...args, attempt.effort)
          : api.structuredCreate(...args));
    clearAttempt(); // only THIS attempt's slot — a lost guarded one stays recoverable (5916)
    // Phase 2 (#1339): only now — the create's response is in hand — does a skip create launch.
    // The page opens at once, and the request's own outcome goes to the session pane under the
    // same key (a refusal shows its reason; a lost answer says so).
    if (attempt.bypass && req.start !== false) startCreated(key);
    return key;
  } catch (e) {
    if (e instanceof ApiError && e.status < 500) clearAttempt(); // refused
    throw e;
  }
}

/** Phase 2 of a skip create (#1339): start the session the create made, fired, not awaited; its
 *  outcome goes to the session pane under the same key. */
export function startCreated(key: string): void {
  handStart(key, api.structuredStart(key));
}
