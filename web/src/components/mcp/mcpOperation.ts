/** A review or install of a package MCP server as a polled operation (#2163 P1b).
 *
 *  Resolving or downloading a package can take minutes, far longer than a proxy or the Home Free
 *  relay keeps one request open, so the server runs it in the background: the POST names the
 *  operation with a client-made `request_id` and answers 202 at once, and the page reads
 *  `GET /api/mcp/operations/{id}` about once a second until it is no longer `running`.
 *
 *  Sending the same `request_id` again returns the same operation and never runs it twice, so a
 *  request lost on the way (no answer, or a gateway's 502/503/504) is retried with the SAME id,
 *  automatically and when the operator presses again. Anything the server answered (a 409 for
 *  another operation on this server, a `failed` operation, a 404 after an app restart) ends the
 *  attempt, and the next press starts a fresh one. */
import { useCallback, useEffect, useRef } from "react";

import { ApiError, api } from "../../lib/api";
import type { McpOperation } from "../../types/mcp";

export const OPERATION_POLL_MS = 1000;
/** A lost POST is sent again this many times (same id) before the page says so. */
export const START_RETRIES = 2;
/** Polls in a row that may get no answer before the page says so (the operation carries on). */
export const POLL_MISSES = 30;
export const OPERATION_LOST = "The operation was lost; try again.";

/** The operation ended on the server: it failed with these words, or the server no longer knows it. */
export class OperationEnded extends Error {}

/** No answer, or a gateway's: the server may or may not have the request, so the same id is safe
 *  (and right) to send again. */
export const transient = (e: unknown): boolean =>
  !(e instanceof OperationEnded) && (!(e instanceof ApiError) || [502, 503, 504].includes(e.status));

export interface OperationIo<T> {
  start: (requestId: string) => Promise<McpOperation<T>>;
  poll: (requestId: string) => Promise<McpOperation<T>>;
  /** Wait `ms`; `false` once the page is gone (stop, send nothing more). */
  wait: (ms: number) => Promise<boolean>;
}

/** Start (or rejoin) operation `requestId` and poll it to its end. Resolves with its result, or
 *  `null` when the page went away first; rejects with the server's words. */
export async function settleOperation<T>(requestId: string, io: OperationIo<T>): Promise<T | null> {
  let op: McpOperation<T>;
  for (let attempt = 0; ; attempt++) {
    try {
      op = await io.start(requestId);
      break;
    } catch (e) {
      if (!transient(e) || attempt >= START_RETRIES) throw e;
      if (!(await io.wait(OPERATION_POLL_MS))) return null;
    }
  }
  let misses = 0;
  while (op.state === "running") {
    if (!(await io.wait(OPERATION_POLL_MS))) return null;
    try {
      op = await io.poll(requestId);
      misses = 0;
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) throw new OperationEnded(OPERATION_LOST);
      if (!transient(e) || ++misses > POLL_MISSES) throw e;
    }
  }
  if (op.state === "failed") throw new OperationEnded(op.error || `The ${op.kind} failed.`);
  return op.result as T;
}

/** One page's operation of one kind. `run(key, start)` starts it under a fresh id, or under the
 *  id of the last attempt with the same `key` if that one was lost on the way; it resolves `null`
 *  if the page unmounted meanwhile (polling stops). The key names what is being done (the server
 *  and its revision, or the review being installed), so a changed source never rejoins an old
 *  review. */
export function useMcpOperation() {
  const alive = useRef(true);
  const pending = useRef<{ key: string; id: string } | null>(null);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return useCallback(async <T,>(key: string, start: (requestId: string) => Promise<McpOperation<T>>): Promise<T | null> => {
    const id = pending.current?.key === key ? pending.current.id : crypto.randomUUID();
    pending.current = { key, id };
    const forget = () => {
      if (pending.current?.id === id) pending.current = null;
    };
    try {
      const result = await settleOperation<T>(id, {
        start,
        poll: (rid) => api.mcpOperation<T>(rid),
        wait: (ms) => new Promise((resolve) => window.setTimeout(() => resolve(alive.current), ms)),
      });
      if (result !== null) forget();
      return result;
    } catch (e) {
      if (!transient(e)) forget();
      throw e;
    }
  }, []);
}
