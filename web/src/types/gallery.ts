import type { PlaybookCardData } from "./playbooks";
export type GalleryKind = "playbook" | "skill";
export type GalleryFiles = Record<string, string | { base64: string }>;
export interface GalleryContent {
  format: 1;
  kind: GalleryKind;
  id: string;
  version: string;
  title: string;
  author: string;
  publisher: string;
  summary: string;
  source: string;
  requirements: string[];
  files: Record<string, string>;
}
export interface GalleryEntry {
  content: GalleryContent;
  digest: string;
  available: boolean;
}
export interface GalleryIndex {
  state: "empty" | "ready" | "stale" | "unavailable";
  entries: GalleryEntry[];
  error: string | null;
  sequence?: number;
  digest?: string;
  issued_at?: number;
  expires_at?: number;
}
export interface GalleryOrigin {
  state: "verified" | "unavailable";
  error?: string;
  pointer?: string;
  original?: string;
  previous?: string | null;
  kind?: GalleryKind;
  id?: string;
  version?: string;
  title?: string;
  publisher?: string;
  source?: string;
  entry_digest?: string;
  base_revision?: string;
}
export interface LocalSkill {
  id: string;
  revision: string;
  files: Record<string, string>;
  gallery_origin?: GalleryOrigin | null;
  editable: boolean;
  error?: string;
  durable?: boolean;
  durable_reason?: string;
}
export interface GalleryInspection {
  preview?: {
    flows: PlaybookCardData["flows"];
    inputs: {
      name: string;
      label?: string;
      required?: boolean;
      type?: string;
    }[];
  } | null;
  entry: GalleryEntry;
  importable: boolean;
  reason: string | null;
  cut_digest: string;
}
export interface GalleryComparison {
  kind: GalleryKind;
  id: string;
  revision: string;
  origin: string;
  incoming_digest: string;
  comparison: string;
  base: GalleryFiles;
  local: GalleryFiles;
  incoming: GalleryFiles;
  proposed: GalleryFiles;
  conflicts: string[];
  changed_files: string[];
  incoming_version: string;
  /** The version this copy was imported from (#1440: "v1.2.0 → v1.3.0"). */
  base_version?: string;
}
export interface GalleryOperation {
  operation: string;
  state: "complete" | "uncertain";
  message?: string;
  result?: {
    kind: GalleryKind;
    id: string;
    revision: string;
    durable?: boolean;
    durable_reason?: string;
    gallery_origin: GalleryOrigin | null;
  };
}
