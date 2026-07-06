/** Display helpers shared across the sidebar. */

export function relTime(epoch: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 60) return "just now";
  if (s < 3600) {
    const m = Math.floor(s / 60);
    return `${m} ${m === 1 ? "min" : "mins"} ago`;
  }
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    return `${h} ${h === 1 ? "hour" : "hours"} ago`;
  }
  const d = Math.floor(s / 86400);
  return `${d} ${d === 1 ? "day" : "days"} ago`;
}

export function shortCwd(cwd: string): string {
  return cwd.replace(/^\/home\/[^/]+\//, "~/");
}

/** Last path segment — the default entity name when promoting a folder cluster to a
 *  project (#361 Phase 4). Falls back to the input for degenerate paths ("/", ""). */
export function pathBase(cwd: string): string {
  return cwd.replace(/\/+$/, "").split("/").pop() || cwd;
}

/** A project's display label: the user's custom name for this cwd (#148) if set, else the
 *  shortened path. Display-only — filtering/identity stays keyed by the raw cwd. */
export function displayProjectName(cwd: string, names?: Record<string, string>): string {
  return names?.[cwd]?.trim() || shortCwd(cwd);
}

export function engineBadge(engine: string): string {
  return engine === "opencode"
    ? "oc"
    : engine === "codex"
      ? "cx"
      : engine === "gemini"
        ? "gm"
        : engine === "antigravity"
          ? "ag" // agy
          : "cc";
}

/** Per-engine accent for the overview chips (tuned for the dark HUD canvas). */
export function engineColor(engine: string): string {
  return engine === "opencode"
    ? "#4fd1c5" // teal
    : engine === "codex"
      ? "#7ee787" // green
      : engine === "gemini"
        ? "#7aa2ff" // blue
        : engine === "antigravity"
          ? "#a78bfa" // violet — agy
          : "#d98a5c"; // claude — amber
}

/** Deterministic per-project accent (#285): FNV-1a over the project key (entity id, or the
 *  cwd for a folder ref) → golden-angle hue. `light-dark()` picks the theme-appropriate
 *  lightness — the app sets `color-scheme` per theme, so the same hue reads on both canvases.
 *  Fallback only — an explicit entity color (Settings, #361) wins at the call sites. */
export function projectColor(key: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  const hue = Math.round(((h >>> 0) * 137.508) % 360);
  return `light-dark(hsl(${hue} 55% 40%), hsl(${hue} 60% 58%))`;
}

/** Human display name for an engine id (sidebar uses short badges; cards want the name). */
export function engineName(engine: string): string {
  return engine === "opencode"
    ? "opencode"
    : engine === "codex"
      ? "codex"
      : engine === "gemini"
        ? "gemini"
        : engine === "antigravity"
          ? "antigravity"
          : engine === "claude"
            ? "claude"
            : engine;
}

/** Humanize a byte count to a compact GB/MB string (binary units). */
export function humanBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  const gb = n / 1024 ** 3;
  if (gb >= 1) return `${gb.toFixed(gb >= 10 ? 0 : 1)} GB`;
  const mb = n / 1024 ** 2;
  return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
}

/** Humanize a duration in seconds to "Xd Yh" / "Yh Zm" / "Zm". */
export function humanDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}
