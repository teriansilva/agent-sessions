/** Installer state comes from /api/plugins; the live engine roster remains engineRoster.ts. */
export interface PluginManifest {
  identity: { id: string; label: string; publisher: string; version: string; kind?: string };
  runtime?: { kind: "pty" | "chat" | "api" };
  /** runtime "api" only (#1311): the native protocol and the console agent it drives. */
  api?: { kind: string; source: string };
  install?: { kind: string; authority?: string; package?: string; version?: string; entrypoint?: string };
  signin?: { kind: string; subcommand?: string };
}
export interface PluginEntry {
  manifest: PluginManifest;
  recipe: { artifacts: { url: string; sha256: string; destination: string }[] };
}
export interface PluginReview {
  id: string;
  plugin_id: string;
  source: "signed" | "local";
  sequence: number | null;
  digest: string;
  recipe_digest: string;
  entry: PluginEntry;
  adopted_path: string | null;
  adopted_sha256: string | null;
  expires_at: number;
}
export interface PluginGeneration {
  id: string;
  review: PluginReview;
  required_checks: string[];
  verification: null | {
    digest: string;
    checked_at: number;
    results: { check: string; passed: boolean; detail: string; version?: string }[];
  };
}
export interface PluginInstallation {
  id: string;
  active?: string | null;
  candidate?: string | null;
  enabled?: boolean | null;
  generations?: PluginGeneration[];
  error?: string;
}
export interface PluginOperation {
  id: string;
  plugin_id: string;
  kind: "install" | "verify" | "signin" | "activate" | "disable" | "remove";
  state: "planned" | "running" | "ready" | "cleanup_pending" | "installed" | "verified" | "complete" | "failed" | "interrupted";
  created_at: number;
  updated_at: number;
  generation_id: string | null;
  review: PluginReview | null;
  review_digest: string | null;
  error: string | null;
}
export interface PluginCatalog {
  feed: { state: "ready" | "missing" | "unavailable"; sequence?: number | null; expires_at?: number | null; digest?: string | null; error: string | null };
  catalog: { manifest: PluginManifest; digest: string }[];
  plugins: PluginInstallation[];
  operations: PluginOperation[];
  roster_generation: number;
  roster_revision: string | null;
}
export interface PluginAction {
  request_id: string;
  plugin_id: string;
  generation_id: string;
}
