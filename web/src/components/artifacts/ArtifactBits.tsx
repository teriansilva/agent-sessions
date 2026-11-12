/** Small shared pieces of the artifacts UI (#1932 P2): the kind tag and the visibility tag. */
import { AppWindow, File, FileImage, FileText, Lock } from "lucide-react";

import type { ArtifactKind, ArtifactVisibility } from "../../types/artifacts";
import { isImageEntry, kindLabel } from "./artifactModel";
import styles from "./artifacts.module.css";

export function KindTag({
  kind,
  entry,
}: {
  kind: ArtifactKind;
  entry?: string | null;
}) {
  const label = kindLabel(kind, entry);
  const Icon =
    kind === "site"
      ? AppWindow
      : isImageEntry(kind, entry)
        ? FileImage
        : /^(PDF|MD|TXT)$/.test(label.toUpperCase())
          ? FileText
          : File;
  return (
    <span
      className={`${styles.kind} ${kind === "site" ? styles.kindSite : ""}`}
      data-kind={kind}
    >
      <Icon size={12} aria-hidden="true" />
      {label}
    </span>
  );
}

/** PRIVATE is the word and a lock in quiet ink; PUBLIC the word and the 6 px accent square. A tag,
 *  never a status colour (decision 3). */
export function VisibilityTag({
  visibility,
}: {
  visibility: ArtifactVisibility;
}) {
  if (visibility === "public")
    return (
      <span
        className={`${styles.vis} ${styles.visPublic}`}
        data-visibility="public"
      >
        <span className={styles.sq} aria-hidden="true" />
        Public
      </span>
    );
  return (
    <span className={styles.vis} data-visibility="private">
      <Lock size={12} aria-hidden="true" />
      Private
    </span>
  );
}
