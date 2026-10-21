// Human label for THIS device, shown on the take-over gate (#293) so you can tell which
// of your devices currently holds a session. Client-chosen and display-only: persisted to
// localStorage, defaulted from the User-Agent, length-capped, and never used for auth (the
// server treats it as an opaque, untrusted string — auth stays the app-session cookie).

const LABEL_KEY = "tr-device-label";
const MAX = 80;

/** Best-effort OS · Browser from the UA, e.g. "iPhone · Safari", "Mac · Chrome". */
function defaultLabel(): string {
  const ua = (typeof navigator !== "undefined" && navigator.userAgent) || "";
  const os = /iPhone/.test(ua)
    ? "iPhone"
    : /iPad/.test(ua)
      ? "iPad"
      : /Android/.test(ua)
        ? "Android"
        : /Macintosh|Mac OS X/.test(ua)
          ? "Mac"
          : /Windows/.test(ua)
            ? "Windows"
            : /Linux/.test(ua)
              ? "Linux"
              : "Device";
  // Order matters: Edge/Chrome UAs also contain "Safari"; check the more specific ones first.
  const br = /Edg\//.test(ua)
    ? "Edge"
    : /OPR\/|Opera/.test(ua)
      ? "Opera"
      : /Firefox\//.test(ua)
        ? "Firefox"
        : /Chrome\//.test(ua)
          ? "Chrome"
          : /Safari\//.test(ua)
            ? "Safari"
            : "Browser";
  return `${os} · ${br}`;
}

/** The label to send on attach — the user's saved name, else the UA default. */
export function getDeviceLabel(): string {
  try {
    const v = localStorage.getItem(LABEL_KEY);
    if (v && v.trim()) return v.trim().slice(0, MAX);
  } catch {
    /* localStorage blocked (private mode) — fall through to the UA default */
  }
  return defaultLabel().slice(0, MAX);
}

/** Persist a user-chosen device name (Settings, Phase 2 follow-up). Empty clears it. */
export function setDeviceLabel(v: string): void {
  try {
    const t = v.trim().slice(0, MAX);
    if (t) localStorage.setItem(LABEL_KEY, t);
    else localStorage.removeItem(LABEL_KEY);
  } catch {
    /* best effort */
  }
}
