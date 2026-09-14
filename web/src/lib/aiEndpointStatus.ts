import { useSyncExternalStore } from "react";

/** What the Settings LED says about the SAVED AI connection (#956).
 *
 *  - `unset`: not configured (no base URL or no key).
 *  - `up`: the last check of this saved connection, in this app visit, succeeded.
 *  - `degraded`: configured but not checked yet in this visit, or the endpoint doesn't list
 *    models (the URL and key can still be right).
 *  - `down`: the last check of this saved connection failed.
 *
 *  Client-only by design: there is no server-side health state. The Endpoint & model page and
 *  its mount-time listing write here; the sidebar and the phone index read it. */
export type EndpointLed = "unset" | "up" | "degraded" | "down";

type CheckRecord = {
  /** The saved origin the check describes. */
  origin: string | null;
  /** Whether a key was stored when it was checked — a key change is a different connection. */
  keySet: boolean;
  status: Exclude<EndpointLed, "unset">;
};

let record: CheckRecord | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** `scheme://host:port` with the default port made explicit — the same normalization as the
 *  server's `prefs.endpoint_origin`, so the page asks for a key exactly when the server would. */
export function endpointOrigin(url: string | null | undefined): string | null {
  if (!url || !url.trim()) return null;
  try {
    const u = new URL(url.trim());
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    const scheme = u.protocol.slice(0, -1);
    const port = u.port || (scheme === "https" ? "443" : "80");
    return `${scheme}://${u.hostname.toLowerCase()}:${port}`;
  } catch {
    return null;
  }
}

/** Record the outcome of checking a SAVED connection. Draft tests never call this: the LED
 *  describes what is saved, not what is being typed. */
export function recordEndpointCheck(
  origin: string | null,
  keySet: boolean,
  status: CheckRecord["status"],
): void {
  record = { origin, keySet, status };
  listeners.forEach((l) => l());
}

/** The LED for a saved connection. A record for another origin, or taken before the key was
 *  added or removed, says nothing about this one — that reads as `degraded` until re-checked. */
export function useEndpointLed(
  block:
    | { base_url: string; api_key_set: boolean; configured: boolean }
    | undefined,
): EndpointLed {
  const current = useSyncExternalStore(
    subscribe,
    () => record,
    () => record,
  );
  if (!block?.configured) return "unset";
  if (
    !current ||
    current.origin !== endpointOrigin(block.base_url) ||
    current.keySet !== block.api_key_set
  ) {
    return "degraded";
  }
  return current.status;
}

/** `.hud-led` modifier per state. Status colour is load-bearing (docs/design.md §3): the
 *  degraded state wears the degraded status tone, never the brand accent. */
export const ENDPOINT_LED_CLASS: Record<EndpointLed, string> = {
  unset: "idle",
  up: "up",
  degraded: "attention",
  down: "down",
};

export const ENDPOINT_LED_LABEL: Record<EndpointLed, string> = {
  unset: "Not set up",
  up: "Connected",
  degraded: "Not verified",
  down: "Check failed",
};

/** Test seam: the store is module state and would otherwise leak between tests. */
export function resetEndpointStatus(): void {
  record = null;
  listeners.forEach((l) => l());
}
