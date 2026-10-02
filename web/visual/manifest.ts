// Pure pieces of the visual capture (#96) — argument parsing + manifest shaping,
// unit-tested in manifest.test.ts. The Playwright orchestration lives in
// web/e2e/visual.spec.ts and consumes these. Mirrors demoapp.io's split so the
// logic is tested without a browser.

import { KNOWN_AREA_KEYS, VIEWPORT_NAMES, type ViewportName } from "./paths.js";

export type AreaParseResult =
  | { kind: "none" }
  | { kind: "invalid"; unknown: string[] }
  | { kind: "ok"; areas: string[] };

/** Parse the --areas / VISUAL_AREAS CSV. `all` → every known key; `none`/empty → exit cleanly. */
export function parseAreasArg(
  raw: string | undefined,
  knownKeys: ReadonlySet<string> = KNOWN_AREA_KEYS,
): AreaParseResult {
  const trimmed = (raw ?? "").trim();
  if (trimmed === "" || trimmed.toLowerCase() === "none") return { kind: "none" };
  if (trimmed.toLowerCase() === "all") return { kind: "ok", areas: Array.from(knownKeys) };
  const tokens = trimmed
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const unknown = tokens.filter((t) => !knownKeys.has(t));
  if (unknown.length > 0) return { kind: "invalid", unknown };
  return { kind: "ok", areas: tokens };
}

export type PathStatus = "ok" | "failed" | "login_failed" | "seed_failed";

export type ManifestEntry = {
  name: string;
  viewport: ViewportName;
  url: string;
  role: "anonymous" | "admin";
  status: PathStatus;
  file: string | null;
  duration_ms: number;
  error?: string;
};

export type Manifest = {
  captured_at: string;
  head_sha: string | null;
  base_url: string;
  resolved_areas: string[];
  viewports: ViewportName[];
  paths: ManifestEntry[];
};

/** Screenshot filename for an area × viewport (stable; the manifest + comment key off it). */
export function shotFile(name: string, viewport: ViewportName): string {
  return `${name}__${viewport}.png`;
}

export function emptyManifest(baseUrl: string, headSha: string | null, areas: string[]): Manifest {
  return {
    captured_at: new Date().toISOString(),
    head_sha: headSha,
    base_url: baseUrl,
    resolved_areas: areas,
    viewports: [...VIEWPORT_NAMES],
    paths: [],
  };
}
