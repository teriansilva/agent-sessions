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
  public_since: number | null;
  files: number;
  bytes: number;
  /** The file the stable link opens (null for a site without an index.html). Absent on a server
   *  from before #1932 P2. */
  entry?: string | null;
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
