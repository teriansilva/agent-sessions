/** Artifacts (#1932 P2): the pure decisions the Library page, the detail, the session tab and the
 *  activation dialogs share, so they say the same thing everywhere and a unit test can hold them.
 *  Nothing here reads the DOM or the network. */
import type { EngineInfo } from "../../types/api";
import type { ArtifactMode, ArtifactKind } from "../../types/artifacts";

/** The preview frame's sandbox (#1932): scripts, forms, popups and downloads, NEVER
 *  `allow-same-origin` — agent-written HTML must stay an opaque origin that cannot read BattleLab's
 *  cookies or any same-origin `/api` answer. The server sends the same sandbox as a CSP header. */
export const PREVIEW_SANDBOX =
  "allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads";

/** The kind tag: SITE for a site, else the entry file's extension (PNG, PDF, MD…), else FILE. */
export function kindLabel(kind: ArtifactKind, entry?: string | null): string {
  if (kind === "site") return "Site";
  const name = (entry ?? "").split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 ? name.slice(dot + 1) : "";
  return /^[a-z0-9]{1,6}$/i.test(ext) ? ext.toUpperCase() : "File";
}

const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "avif", "svg"]);
/** Whether a FILE artifact's entry is an image the session tab may show as a thumbnail. */
export function isImageEntry(
  kind: ArtifactKind,
  entry?: string | null,
): boolean {
  if (kind !== "file" || !entry) return false;
  const ext = entry.split(".").pop()?.toLowerCase() ?? "";
  return IMAGE_EXT.has(ext);
}

/** Bytes as the boards write them: `14 KB`, `2.4 MB`, `5 GB`, one decimal under 10. Binary steps,
 *  like the server's caps (200 MB is 200 × 2^20 bytes), so a cap reads as the round number it is. */
export function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 ? v.toFixed(1).replace(/\.0$/, "") : Math.round(v)} ${units[i]}`;
}

export const filesLabel = (n: number) =>
  `${n.toLocaleString("en-US")} ${n === 1 ? "file" : "files"}`;

/** A revision's label, lower-case everywhere (Hermes on the P0 boards). */
export const revLabel = (n: number) => `r${n}`;

/** The three activation modes as the control words them (board 4, revision 3). */
export const MODES: readonly {
  value: ArtifactMode;
  label: string;
  text: string;
}[] = [
  { value: "off", label: "Off", text: "The agent does not get the tool." },
  {
    value: "private",
    label: "Private",
    text: "The agent can publish. Links are private to you; only you can share them publicly.",
  },
  {
    value: "public",
    label: "Public",
    text: "As Private, and the agent can also share publicly itself (artifact_share_public). In any other mode the server refuses that request.",
  },
];

export const modeLabel = (m: ArtifactMode) =>
  MODES.find((x) => x.value === m)?.label ?? m;

/** Whether an agent has a BattleLab tool channel: its manifest declares `tools.mcp`. `undefined`
 *  when the roster does not know the engine (or is not loaded): then nothing is claimed either
 *  way, and the control stays usable — the server's broker is the boundary. */
export function hasToolChannel(
  info: EngineInfo | undefined,
): boolean | undefined {
  if (!info) return undefined;
  if (info.tools === undefined) return undefined; // an older server: the fact is not exposed
  return typeof info.tools.mcp === "string" && info.tools.mcp.length > 0;
}

export type RestartChange = "on" | "mode" | "off";

/** What a running agent's restart changes, from its effective mode before and after an edit. Off →
 *  on: it has no tool yet. Private ↔ Public: it has the tool, the new mode applies at restart. On →
 *  off: it keeps the tool until it restarts. `null`: nothing changes for it. */
export function restartChange(
  before: ArtifactMode,
  after: ArtifactMode,
): RestartChange | null {
  if (before === after) return null;
  if (before === "off") return "on";
  if (after === "off") return "off";
  return "mode";
}

/** The restart notice's second line, naming the agent (`claude`, `codex`…). */
export function restartLine(change: RestartChange, who: string): string {
  if (change === "on")
    return `The running ${who} does not have the tool yet; it gets it at restart.`;
  if (change === "mode")
    return `The new mode applies when the ${who} restarts.`;
  return `The running ${who} keeps the tool until it restarts.`;
}

/** Where a session's effective mode comes from, in words, for the line under the control. */
export function sourceWords(source: string): string {
  switch (source) {
    case "session":
      return "set for this session";
    case "mission":
      return "from its mission";
    case "project":
      return "from the project default";
    case "global":
      return "Artifacts are off in Settings";
    default:
      return "nothing is set, so it is off";
  }
}

/** Is an error the server's "public links are off on this box" (409 `public_off`)? */
export function isPublicOff(e: unknown): boolean {
  if (!e || typeof e !== "object") return false;
  const rec = (e as { record?: unknown; status?: number }).record;
  return (
    (e as { status?: number }).status === 409 &&
    !!rec &&
    typeof rec === "object" &&
    (rec as { detail?: unknown }).detail === "public_off"
  );
}

/** Public links exist only with a login; `auth_mode` undefined (config not read yet) claims nothing. */
export function publicLinksOff(authMode: string | undefined): boolean {
  return authMode === "none";
}

/** An absolute link to show and copy: the server's own (absolute when it knows its origin), else
 *  this page's origin plus the path. */
export function absoluteLink(url: string, origin: string): string {
  if (/^https?:\/\//i.test(url)) return url;
  return `${origin.replace(/\/$/, "")}${url.startsWith("/") ? "" : "/"}${url}`;
}

/** The id's hyphens become break opportunities, so a phone wraps the link only there. */
export function breakAtHyphens(url: string): string {
  return url.replace(/-/g, "-​");
}

/** "12 min ago" — the boards' short form of the app's relative time. */
export function ago(epoch: number, now = Date.now() / 1000): string {
  const s = Math.max(0, Math.floor(now - epoch));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}
