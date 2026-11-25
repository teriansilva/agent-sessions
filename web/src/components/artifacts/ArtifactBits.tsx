/** Small shared pieces of the artifacts UI (#1932 P2): the kind tag, the visibility tag and what a
 *  public link carries (#2217). */
import { AppWindow, File, FileImage, FileText, Lock } from "lucide-react";

import { formatWhen, nowSeconds } from "../../lib/shareFormat";
import type {
  Artifact,
  ArtifactDetail,
  ArtifactKind,
  ArtifactVisibility,
} from "../../types/artifacts";
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

/** What a box link carries (#2217): PASSWORD, EXPIRES IN …, NO HEADER on a live one; LINK EXPIRED
 *  on one that reached its expiry. Nothing for a plain link or a private artifact. Tags in quiet
 *  ink; only the expired one is set off, as a warning, never a status colour. `compact` is the
 *  list's row (short words). */
export function LinkFacts({
  artifact: a,
  compact = false,
}: {
  artifact: Artifact | ArtifactDetail;
  compact?: boolean;
}) {
  const facts: {
    key: string;
    label: string;
    title?: string;
    warn?: boolean;
  }[] = [];
  if (a.public_expired && a.public_expires_at) {
    facts.push({
      key: "expired",
      label: compact
        ? "Link expired"
        : `Expired ${formatWhen(a.public_expires_at)}`,
      title: formatWhen(a.public_expires_at),
      warn: true,
    });
  } else if (a.visibility === "public") {
    if (a.public_has_password)
      facts.push({ key: "password", label: "Password" });
    if (a.public_expires_at)
      facts.push({
        key: "expires",
        label: compact
          ? untilLabel(a.public_expires_at)
          : `Expires in ${untilLabel(a.public_expires_at)}`,
        title: formatWhen(a.public_expires_at),
      });
    if (a.public_header === false)
      facts.push({ key: "header", label: "No header" });
  }
  if (!facts.length) return null;
  return (
    <span className={styles.linkFacts} data-testid="artifact-link-facts">
      {facts.map((f) => (
        <span
          key={f.key}
          className={`${styles.fact} ${f.warn ? styles.factWarn : ""}`}
          title={f.title}
          data-fact={f.key}
        >
          {f.label}
        </span>
      ))}
    </span>
  );
}

/** "7d", "5h", "12m": how long until a future time, in the largest whole unit. */
function untilLabel(epochS: number): string {
  const s = Math.max(0, epochS - nowSeconds());
  if (s >= 86400) return `${Math.floor(s / 86400)}d`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h`;
  return `${Math.max(1, Math.floor(s / 60))}m`;
}
