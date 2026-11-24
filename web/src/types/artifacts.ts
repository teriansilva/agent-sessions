/** Artifacts (#1932): files and static sites an agent hosts on BattleLab, as the operator API
 *  (`src/agent_sessions/routes/artifacts.py`, `artifact_store.view`) returns them. */

export type ArtifactKind = "file" | "site";
export type ArtifactVisibility = "private" | "public";
export type ArtifactMode = "off" | "private" | "public";
export type ActivationScope = "session" | "mission" | "project";

export interface ArtifactOrigin {
  engine: string;
  session_key: string;
  cwd: string;
  project_id: string | null;
  mission_id: string | null;
}

/** One artifact as `GET /api/artifacts` lists it. `files` and `bytes` are its latest revision's. */
export interface Artifact {
  id: string;
  title: string;
  why: string;
  kind: ArtifactKind;
  revision: number;
  created_at: number;
  updated_at: number;
  origin: ArtifactOrigin;
  visibility: ArtifactVisibility;
  /** The operator's stable link, absolute when the server knows its origin. */
  url: string;
  /** Only while the artifact is shared. */
  public_url: string | null;
  /** Home Free (#2190): a first upload to the relay did not confirm, so a public copy may exist
   *  without its link being known here. Revoke (or share again) settles it. */
  relay_uncertain?: boolean;
  public_since: number | null;
  /** A box link's options (#2217). Absent on a server from before them. */
  public_has_password?: boolean;
  /** When the box link stops (or stopped) working; null = never. */
  public_expires_at?: number | null;
  /** The box link reached its expiry: it opens nothing and the artifact is private again. */
  public_expired?: boolean;
  /** False: the link opens without the BattleLab header. */
  public_header?: boolean;
  files: number;
  bytes: number;
  /** The file the stable link opens (null for a site without an index.html). Absent on a server
   *  from before #1932 P2. */
  entry?: string | null;
}

/** A public link's options (#2217), set when the link is made. Omitted = today's link. */
export interface ArtifactShareOptions {
  password?: string;
  expires_at?: number | null;
  header?: boolean;
}

export interface ArtifactRevision {
  n: number;
  created_at: number;
  files: number;
  bytes: number;
}

export interface ArtifactFile {
  path: string;
  size: number;
  /** The served type, or null when the file downloads. */
  type: string | null;
}

/** `GET /api/artifacts/{id}`: the latest revision's files replace the count. */
export interface ArtifactDetail extends Omit<Artifact, "files" | "bytes"> {
  revisions: ArtifactRevision[];
  files?: ArtifactFile[];
}

export interface ArtifactPage {
  items: Artifact[];
  total: number;
  next_cursor: string | null;
}

export interface ArtifactQuery {
  project?: string;
  session?: string;
  mission?: string;
  kind?: ArtifactKind;
  visibility?: ArtifactVisibility;
  q?: string;
  cursor?: string;
}

export interface ArtifactCaps {
  revision_bytes: number;
  revision_files: number;
  revisions: number;
  total_bytes: number;
}

/** `GET /api/artifacts/settings`: the global switch plus the store's totals and caps. */
export interface ArtifactSettings {
  enabled: boolean;
  artifacts: number;
  public: number;
  bytes: number;
  caps: ArtifactCaps;
}

/** A shared artifact repository hosted on this BattleLab (#2347 P1), as `GET /api/repos` lists it.
 *  `bytes` counts each blob its revisions name once; `quota_bytes` is its cap. */
export interface ArtifactRepo {
  id: string;
  name: string;
  description: string;
  quota_bytes: number;
  bytes: number;
  items: number;
  created_at: number;
  updated_at: number;
}

export interface ArtifactRepoList {
  repositories: ArtifactRepo[];
  quota_default: number;
}

/** Who added an item: the operator, or an agent session on this BattleLab (by its key). */
export type RepoAddedBy =
  | { kind: "operator" }
  | { kind: "session"; session_key: string };

/** One item of a repository; `files`, `bytes` and `entry` are its latest revision's. */
export interface ArtifactRepoItem {
  id: string;
  repository_id: string;
  title: string;
  why: string;
  kind: ArtifactKind;
  revision: number;
  added_by: RepoAddedBy;
  source_artifact: string | null;
  created_at: number;
  updated_at: number;
  files: number;
  bytes: number;
  entry: string | null;
  url: string;
}

export interface ArtifactRepoItemPage {
  items: ArtifactRepoItem[];
  total: number;
  next_cursor: string | null;
}

/** `GET /api/artifacts/activation`. `mode` null = nothing set there (it inherits). `effective`
 *  only for a session: what its NEXT launch gets, and where that comes from. */
export interface ArtifactActivation {
  scope: ActivationScope;
  id: string;
  mode: ArtifactMode | null;
  enabled: boolean;
  effective?: {
    mode: ArtifactMode;
    source: "global" | "session" | "mission" | "project" | "default";
  };
}
