import { useSyncExternalStore } from "react";

import type { Telemetry } from "../types/api";
import { api } from "./api";

/** How often the shell asks, while the tab is visible (#1531). */
export const TELEMETRY_POLL_MS = 5000;

export type TelemetryState =
  | { kind: "loading" }
  | { kind: "ok"; data: Telemetry }
  | { kind: "unavailable" };

/** ONE poller for every reader (the footer readout, the top bar's uplink state): `GET
 *  /api/telemetry` every 5 s while at least one component is subscribed AND the tab is visible —
 *  a hidden tab asks nothing, and asks at once when it comes back. A failed read keeps the last
 *  good value for one interval before saying "unavailable", so one dropped request does not
 *  flicker the bar. */
let state: TelemetryState = { kind: "loading" };
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setTimeout> | undefined;
let inFlight = false;
let misses = 0;

const hidden = () => typeof document !== "undefined" && document.visibilityState === "hidden";

function set(next: TelemetryState) {
  state = next;
  for (const l of listeners) l();
}

async function tick() {
  timer = undefined;
  if (!listeners.size || hidden() || inFlight) return;
  inFlight = true;
  try {
    const data = await api.telemetry();
    misses = 0;
    set({ kind: "ok", data });
  } catch {
    misses += 1;
    if (misses > 1 || state.kind === "loading") set({ kind: "unavailable" });
  } finally {
    inFlight = false;
  }
  if (listeners.size && !hidden()) timer = setTimeout(tick, TELEMETRY_POLL_MS);
}

function onVisibility() {
  if (!hidden() && !timer && !inFlight) void tick();
  else if (hidden() && timer) {
    clearTimeout(timer);
    timer = undefined;
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    document.addEventListener("visibilitychange", onVisibility);
    if (!timer && !inFlight) void tick();
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      document.removeEventListener("visibilitychange", onVisibility);
      if (timer) clearTimeout(timer);
      timer = undefined;
    }
  };
}

export function useTelemetry(): TelemetryState {
  return useSyncExternalStore(subscribe, () => state, () => state);
}

/** Test seam: forget everything (the store is module state). */
export function resetTelemetryForTests(): void {
  if (timer) clearTimeout(timer);
  timer = undefined;
  inFlight = false;
  misses = 0;
  state = { kind: "loading" };
  listeners.clear();
}

/** 18234 → "18.2K", 412000 → "412K", 1_250_000 → "1.25M". */
export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 100_000) return `${(n / 1000).toFixed(1)}K`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}K`;
  return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 2 : 1)}M`;
}

/** Bytes → "9.8" GB (one decimal), the unit the footer prints after the pair. */
export function gb(bytes: number): string {
  return (bytes / 2 ** 30).toFixed(bytes >= 100 * 2 ** 30 ? 0 : 1);
}
