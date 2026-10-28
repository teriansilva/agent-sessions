/** Share bundles (#1714): the shapes `/api/share/*` answers. */

export type ShareKind = "skill" | "playbook";

/** One exportable local item and the items it needs (`"<kind>:<id>"` keys). */
export interface ShareCatalogItem {
  kind: string;
  id: string;
  title: string;
  depends_on: string[];
  exportable: boolean;
  reason: string | null;
  source: string;
}

/** Copies created from a file: `"<kind>:<id>" → {bundle digest, revision}`. */
export type ShareImports = Record<string, { bundle: string | null; revision: string | null }>;

export interface ShareCatalog {
  items: ShareCatalogItem[];
  imports: ShareImports;
}

export interface SharePreviewFile {
  path: string;
  size: number;
  text?: string;
  binary?: boolean;
  omitted?: boolean;
}

export interface SharePreviewItem {
  key: string;
  kind: string;
  id: string;
  title: string;
  dest: string | null;
  status: "new" | "renamed" | "refused";
  reason: string | null;
  depends_on: string[];
  uses: { from: string; to: string }[];
  files: SharePreviewFile[];
}

export interface SharePreview {
  token: string;
  expires_at: string;
  bundle: {
    digest: string;
    size: number;
    format: number;
    created_at: string;
    app_version: string;
    source: string | null;
    items: number;
    files: number;
  };
  items: SharePreviewItem[];
}

export type ShareOutcome =
  | "created"
  | "refused"
  | "failed"
  | "skipped"
  | "not_selected"
  | "stale";

export interface ShareResultItem {
  key: string;
  kind: string;
  id: string;
  title: string;
  status: ShareOutcome;
  dest: string | null;
  reason: string | null;
}

export interface ShareResult {
  token: string;
  bundle: string;
  repreview: "expired" | "changed" | "evicted" | null;
  items: ShareResultItem[];
}
