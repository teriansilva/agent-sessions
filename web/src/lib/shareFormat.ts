/** Public share links (#1404 P4): the words both sides use for a link — the operator's dialog and
 *  Settings → Sharing, and the visitor's viewer. Pure: no React, no fetch, no import from the
 *  operator app, so the viewer bundle can use it without pulling the operator's code in. Times are
 *  epoch SECONDS, as the server sends them. */

export type ShareMode = "view" | "send";
export type ShareKind = "api" | "terminal";

/** An operator-facing link row (`Share.public()`): never the token. */
export interface PublicShare {
  share_id: string;
  session_key: string;
  kind: ShareKind;
  mode: ShareMode;
  title: string;
  has_password: boolean;
  expires_at: number | null;
  created_at: number;
  revoked_at: number | null;
  revoked_reason: string | null;
  open_count: number;
  last_opened_at: number | null;
}

export interface ShareList {
  /** False in `AUTH_MODE=none`: nothing new can be made, existing links can still be revoked. */
  available: boolean;
  reason: string | null;
  shares: PublicShare[];
}

export interface CreatedShare {
  share: PublicShare;
  /** The link. The server returns it ONCE, in this response, and keeps only its digest. */
  url: string;
  /** A `send` link restates the risk the operator accepted. */
  risk?: string;
}

export type ExpiryPreset = "never" | "1h" | "1d" | "7d" | "date";

export const EXPIRY_PRESETS: { id: ExpiryPreset; label: string }[] = [
  { id: "never", label: "Never" },
  { id: "1h", label: "1 hour" },
  { id: "1d", label: "1 day" },
  { id: "7d", label: "7 days" },
  { id: "date", label: "Pick a date…" },
];

const PRESET_S: Record<Exclude<ExpiryPreset, "never" | "date">, number> = {
  "1h": 3600,
  "1d": 86400,
  "7d": 7 * 86400,
};

/** The `expires_at` (epoch seconds) a preset asks for; null = never, NaN = a picked date that
 *  isn't a valid time. `picked` is an `<input type="datetime-local">` value, read as local time. */
export function expiryFor(preset: ExpiryPreset, nowS: number, picked = ""): number | null {
  if (preset === "never") return null;
  if (preset === "date") {
    const ms = picked ? new Date(picked).getTime() : NaN;
    return Number.isFinite(ms) ? Math.floor(ms / 1000) : NaN;
  }
  return Math.floor(nowS) + PRESET_S[preset];
}

const dayMonth = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const fullTime = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

/** "13 Oct 2026, 14:20" in the browser's time zone. */
export function formatWhen(epochS: number): string {
  return fullTime.format(new Date(epochS * 1000));
}

/** How long is left, coarsely: "40 MIN", "23 H", "6 D". Never "0": under a minute is "1 MIN". */
export function timeLeft(expiresAtS: number, nowS: number): string {
  const left = Math.max(0, expiresAtS - nowS);
  if (left < 3600) return `${Math.max(1, Math.floor(left / 60))} MIN`;
  if (left < 86400) return `${Math.floor(left / 3600)} H`;
  return `${Math.floor(left / 86400)} D`;
}

/** The expiry chip: "EXPIRES IN 6 D" / "NEVER EXPIRES". */
export function expiryChip(expiresAtS: number | null, nowS: number): string {
  return expiresAtS == null ? "NEVER EXPIRES" : `EXPIRES IN ${timeLeft(expiresAtS, nowS)}`;
}

/** "just now", "5 min ago", "2 h ago", "yesterday", "3 d ago". */
export function ago(epochS: number, nowS: number): string {
  const d = Math.max(0, nowS - epochS);
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  if (d < 2 * 86400) return "yesterday";
  return `${Math.floor(d / 86400)} d ago`;
}

/** "Created 6 Oct". */
export function createdLabel(createdAtS: number): string {
  return `Created ${dayMonth.format(new Date(createdAtS * 1000))}`;
}

/** "not opened yet" / "opened 4× · last 2 h ago". */
export function openedLabel(share: Pick<PublicShare, "open_count" | "last_opened_at">, nowS: number): string {
  if (!share.open_count) return "not opened yet";
  const last = share.last_opened_at != null ? ` · last ${ago(share.last_opened_at, nowS)}` : "";
  return `opened ${share.open_count}×${last}`;
}

/** The access chip, in the words the dialog's tiles use. */
export function modeChip(mode: ShareMode): string {
  return mode === "send" ? "CAN SEND MESSAGES" : "VIEW ONLY";
}

export function nowSeconds(): number {
  return Date.now() / 1000;
}
