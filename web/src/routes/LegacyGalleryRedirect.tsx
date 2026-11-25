/** Old `/library/gallery[/<digest>][?kind=&id=]` links (#1440). The gallery now lives inside
 *  Library → Playbooks and → Skills; this reads what the link names and lands there with a note.
 *  An invalid kind is never a destination, an entry's kind comes from the signed listing (never the
 *  URL), and only an ID matching the local-ID pattern is ever shown back. */
import { useEffect, useRef } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import {
  GALLERY_PLAYBOOKS_PATH,
  galleryEntryPath,
  galleryUpdatePath,
  PLAYBOOKS_PATH,
  SKILLS_PATH,
} from "../lib/routes";
import type {
  GalleryIndex,
  GalleryKind,
  GalleryOrigin,
} from "../types/gallery";
import {
  classifyLegacy,
  updateStatus,
} from "../components/gallery/galleryRules";
import { ensureChecked } from "../components/gallery/useGallery";
import { localPath } from "../components/gallery/operations";
import type { GalleryLandingState } from "../components/gallery/ViewTabs";

export const MOVED_NOTE = "The gallery now lives in Playbooks and Skills.";
export const INVALID_NOTE =
  "That gallery link doesn't point to anything any more.";

async function catalog(): Promise<GalleryIndex | null> {
  try {
    return await api.publicGallery();
  } catch {
    return null;
  }
}

/** The local copy `id` of `kind` and its gallery origin, `undefined` when there is none. */
async function localCopy(
  kind: GalleryKind,
  id: string,
): Promise<{ origin: GalleryOrigin | null } | undefined> {
  if (kind === "playbook") {
    const list = await api.playbooks();
    const card = list.playbooks.find(
      (p) => p.id === id && p.source === "local",
    );
    return card && { origin: card.gallery_origin ?? null };
  }
  const list = await api.localSkills();
  const skill = list.skills.find((s) => s.id === id);
  return skill && { origin: skill.gallery_origin ?? null };
}

async function resolve(
  digest: string | undefined,
  search: URLSearchParams,
): Promise<[string, string]> {
  const link = classifyLegacy(digest, search);
  if (link.type === "browse") return [GALLERY_PLAYBOOKS_PATH, MOVED_NOTE];
  if (link.type === "invalid") return [GALLERY_PLAYBOOKS_PATH, INVALID_NOTE];
  const yours = (kind: GalleryKind) =>
    kind === "playbook" ? PLAYBOOKS_PATH : SKILLS_PATH;
  if (link.type === "missing-local") return [yours(link.kind), INVALID_NOTE];
  if (link.type === "entry") {
    await ensureChecked();
    const index = await catalog();
    const entry = index?.entries.find((e) => e.digest === link.digest);
    if (entry)
      return [galleryEntryPath(entry.content.kind, entry.digest), MOVED_NOTE];
    // Nothing loaded yet (unreachable, never checked): open it where the listing will load. The
    // playbook view moves to the skill view if the loaded entry turns out to be a skill.
    if (!index?.entries.length)
      return [galleryEntryPath("playbook", link.digest), MOVED_NOTE];
    return [GALLERY_PLAYBOOKS_PATH, INVALID_NOTE];
  }
  let copy;
  try {
    copy = await localCopy(link.kind, link.id);
  } catch {
    return [yours(link.kind), INVALID_NOTE];
  }
  if (!copy)
    return [yours(link.kind), `No local ${link.kind} called “${link.id}”.`];
  // Review update only on a listing this page's check confirmed; otherwise the copy itself.
  const problem = await ensureChecked();
  const status = updateStatus(copy.origin, problem ? null : await catalog());
  if (status?.state === "available")
    return [
      galleryUpdatePath(link.kind, status.entry.digest, link.id),
      MOVED_NOTE,
    ];
  return [localPath(link.kind, link.id), MOVED_NOTE];
}

export default function LegacyGalleryRedirect() {
  const { digest } = useParams();
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void resolve(digest, search).then(([to, galleryNote]) => {
      const state: GalleryLandingState = { galleryNote };
      navigate(to, { replace: true, state });
    });
  }, [digest, search, navigate]);
  return (
    <p role="status" style={{ padding: 24 }}>
      Opening the gallery…
    </p>
  );
}
