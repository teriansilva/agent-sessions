import type { SessionCapacityFacts } from "../types/resources";
import { ApiError } from "./api";

/** The session limit's refusal, wherever a start was refused (#1819).
 *  `session_capacity.SESSION_LIMIT` on a dispatch; HTTP 429 from the API session routes, where no
 *  other refusal uses that status. */
export const SESSION_LIMIT_REFUSAL = "session_limit";

export function isSessionLimit(e: unknown): boolean {
  return e instanceof ApiError && e.status === 429;
}

type Listener = (retry: (() => void) | null) => void;
let listener: Listener | null = null;

/** Open the limit dialog (one instance, mounted by the app shell). `retry` is what its Try again
 *  does; without one it only frees a slot, and the operator starts the thing again themselves. */
export function openSessionLimit(retry?: () => void): void {
  listener?.(retry ?? null);
}

export function listenSessionLimit(next: Listener): () => void {
  listener = next;
  return () => {
    if (listener === next) listener = null;
  };
}

export const gib = (bytes: number | null): string =>
  bytes === null ? "unknown" : `${Math.round(bytes / 1024 ** 3)} GiB`;

/** The one sentence that says why the recommendation is what it is (#1819). */
export function capacityReason(capacity: SessionCapacityFacts): string {
  const { limited_by: by, by_cpu: cpu, by_memory: memory, reserved_bytes: kept } = capacity;
  if (by === "memory") {
    const other = cpu === null ? "" : ` CPUs alone would allow ${cpu}, at 6 per CPU.`;
    return `Memory sets this limit: 1 GiB per session, after keeping ${gib(kept)} for the system.${other}`;
  }
  if (by === "cpu") {
    const other =
      memory === null
        ? " This system's memory couldn't be read."
        : ` Memory alone would allow ${memory}, at 1 GiB per session after keeping ${gib(kept)} for the system.`;
    return `CPU sets this limit: 6 sessions per CPU.${other}`;
  }
  return "This system's memory and CPUs couldn't be read, so this is a cautious default.";
}
