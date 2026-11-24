/** Shared artifact repositories (#2347 P1): the pure wording the Repositories tab, a repository's
 *  page and its dialogs share. Nothing here reads the DOM or the network. */
import type { ArtifactRepo, RepoAddedBy } from "../../types/artifacts";
import { formatSize } from "./artifactModel";

/** The server's rule (`repo_store.NAME_RE`), so the dialog can say it before the request. The
 *  server decides; this only words the hint. */
export const REPO_NAME_RE = /^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$/;
export const REPO_NAME_HINT =
  "Lowercase letters, digits, dots, dashes or underscores; agents name it in artifact_publish.";

export const itemsLabel = (n: number) =>
  `${n.toLocaleString("en-US")} ${n === 1 ? "item" : "items"}`;

/** `412 MB of 2 GB`: the repository's distinct bytes against its quota. */
export const storageLabel = (bytes: number, quota: number) =>
  `${formatSize(bytes)} of ${formatSize(quota)}`;

/** A repository row's facts: `38 items · 412 MB of 2 GB`. */
export const repoFacts = (r: ArtifactRepo) =>
  `${itemsLabel(r.items)} · ${storageLabel(r.bytes, r.quota_bytes)}`;

/** Who added an item, as the page words it: always this BattleLab in P1, then you or a session
 *  (its title when known, else its key). */
export function addedByLabel(
  by: RepoAddedBy,
  sessionTitle?: string | null,
): { who: string; how: string } {
  if (by.kind === "operator") return { who: "this BattleLab", how: "you" };
  return { who: "this BattleLab", how: sessionTitle || by.session_key };
}
