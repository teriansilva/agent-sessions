/** Pure helpers for the Files viewer's media stage (#1530), split from `MediaPreview.tsx` so that
 *  file exports components only (the fast-refresh rule) and so the status mapping is unit-testable
 *  without a DOM. */

export type MediaKind = "image" | "video" | "audio";

/** How many times a busy refusal is retried on its own before the viewer waits for a tap. */
export const BUSY_TRIES = 3;

/** How many times a failed element is re-requested when its probe finds the bytes reachable. */
export const REACHABLE_RETRIES = 1;

export type Phase =
  | { k: "loading" }
  | { k: "ready" }
  | { k: "busy"; tries: number; waitS: number | null }
  | { k: "cantplay" }
  | { k: "unavailable"; message: string }
  | { k: "toolarge" }
  | { k: "timedout" };

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function fmtDuration(s: number): string {
  if (!Number.isFinite(s)) return "";
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

export function subtype(mime: string | undefined): string {
  const sub = (mime ?? "").split("/")[1] ?? "";
  return sub.replace(/^svg\+xml$/, "svg").replace(/^vnd\.microsoft\.icon$/, "ico").toUpperCase();
}

/** Delay before busy retry `tries` (0-based): the server's Retry-After, doubling each time. */
export function busyDelayMs(retryAfter: number | null, tries: number): number {
  return (retryAfter ?? 3) * 1000 * 2 ** tries;
}

/** The status probe's answer → what the viewer says (#1530). A media element's `error` cannot see
 *  the HTTP status, so the viewer asks with one byte through the same transport and maps it. */
export function phaseForProbe(
  probe: { status: number; retryAfter: number | null; detail: string },
  tries: number,
): Phase {
  if (probe.status === 503) return { k: "busy", tries, waitS: probe.retryAfter };
  if (probe.status === 200 || probe.status === 206) return { k: "cantplay" };
  return {
    k: "unavailable",
    message: probe.detail || `The file could not be read (${probe.status}).`,
  };
}
