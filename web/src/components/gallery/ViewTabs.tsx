/** "Your playbooks | Browse gallery" (and the Skills pair) — the two views one Library entry holds
 *  (#1440). Plain links, so each view keeps its own URL and the back button works. */
import { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { X } from "lucide-react";
import { galleryPath, PLAYBOOKS_PATH, SKILLS_PATH } from "../../lib/routes";
import type { GalleryKind } from "../../types/gallery";
import styles from "./viewTabs.module.css";

export function ViewTabs({ kind, view }: { kind: GalleryKind; view: "local" | "gallery" }) {
  const local = kind === "playbook" ? PLAYBOOKS_PATH : SKILLS_PATH;
  return (
    <nav className={styles.tabs} aria-label={kind === "playbook" ? "Playbook views" : "Skill views"}>
      <Link className={styles.tab} to={local} aria-current={view === "local" ? "page" : undefined}>
        {kind === "playbook" ? "Your playbooks" : "Your skills"}
      </Link>
      <Link
        className={styles.tab}
        to={galleryPath(kind)}
        aria-current={view === "gallery" ? "page" : undefined}
      >
        Browse gallery
      </Link>
    </nav>
  );
}

/** Location state an old gallery link carries to where it landed (`LegacyGalleryRedirect`). */
export interface GalleryLandingState {
  galleryNote?: string;
}

/** The one-line, dismissible note an old gallery link lands with. The text is composed by the
 *  redirect from fixed strings (and an ID it already validated); React renders it as text. */
export function LandingNote() {
  const state = useLocation().state as GalleryLandingState | null;
  const note = typeof state?.galleryNote === "string" ? state.galleryNote : "";
  const [closed, setClosed] = useState<string | null>(null);
  if (!note || closed === note) return null;
  return (
    <p className={styles.note} role="status" data-testid="gallery-landing-note">
      <span>{note}</span>
      <button type="button" aria-label="Dismiss" onClick={() => setClosed(note)}>
        <X size={16} aria-hidden />
      </button>
    </p>
  );
}
