/** One shared artifact repository, host side (#2347 P1, mockup frame 3 without its access parts,
 *  which come with peers): its storage against its quota, its items (kind, who added it, size,
 *  revision), Open (the item's latest revision, sandboxed like an artifact), "Add an artifact…",
 *  Remove item behind a confirm, and Delete repository. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, ExternalLink, Plus, Trash2 } from "lucide-react";

import { ApiError, api } from "../lib/api";
import { ARTIFACT_REPOS_PATH, hostedRepoItemPath } from "../lib/routes";
import { appUrl } from "../lib/appBase";
import type {
  ArtifactRepo as Repo,
  ArtifactRepoItem,
  ArtifactRepoItemPage,
} from "../types/artifacts";
import { Callsign, Panel } from "../components/hud/Panel";
import { Loader } from "../components/hud/Loader";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { KindTag } from "../components/artifacts/ArtifactBits";
import { StoreGauge } from "../components/artifacts/StoreGauge";
import { AddArtifactDialog } from "../components/artifacts/RepoDialogs";
import { useArtifactNames } from "../components/artifacts/useArtifactNames";
import { ago, formatSize, revLabel } from "../components/artifacts/artifactModel";
import { addedByLabel, itemsLabel, storageLabel } from "../components/artifacts/repoModel";
import styles from "../components/artifacts/artifacts.module.css";

const message = (e: unknown) =>
  e instanceof ApiError || e instanceof Error
    ? e.message
    : "The request failed. Try again.";

type Confirm =
  | { kind: "remove"; item: ArtifactRepoItem; returnTo: HTMLElement | null }
  | { kind: "delete"; returnTo: HTMLElement | null };

export default function ArtifactRepo() {
  const { repoId = "" } = useParams();
  const navigate = useNavigate();
  const [repo, setRepo] = useState<Repo | null>(null);
  const [page, setPage] = useState<ArtifactRepoItemPage | null>(null);
  const [error, setError] = useState("");
  const [missing, setMissing] = useState(false);
  const [status, setStatus] = useState("");
  const [adding, setAdding] = useState<{ returnTo: HTMLElement | null } | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(
    () =>
      Promise.all([api.artifactRepo(repoId), api.artifactRepoItems(repoId)]).then(
        ([r, p]) => {
          setRepo(r);
          setPage(p);
          setError("");
        },
        (e: unknown) => {
          if (e instanceof ApiError && e.status === 404) setMissing(true);
          else setError(message(e));
        },
      ),
    [repoId],
  );
  useEffect(() => {
    void load();
  }, [load]);

  const items = useMemo(() => page?.items ?? [], [page]);
  const names = useArtifactNames(
    useMemo(
      () =>
        items.flatMap((i) => (i.added_by.kind === "session" ? [i.added_by.session_key] : [])),
      [items],
    ),
  );

  const loadMore = async () => {
    if (!page?.next_cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const next = await api.artifactRepoItems(repoId, page.next_cursor);
      setPage({ ...next, items: [...page.items, ...next.items] });
    } catch (e) {
      setStatus(message(e));
    } finally {
      setLoadingMore(false);
    }
  };

  const act = async () => {
    if (!confirm || busy || !repo) return;
    setBusy(true);
    try {
      if (confirm.kind === "remove") {
        await api.removeFromArtifactRepo(repo.id, confirm.item.id);
        setStatus(`“${confirm.item.title}” is removed from ${repo.name}.`);
        setConfirm(null);
        await load();
      } else {
        await api.deleteArtifactRepo(repo.id);
        navigate(ARTIFACT_REPOS_PATH);
      }
    } catch (e) {
      setStatus(message(e));
      setConfirm(null);
    } finally {
      setBusy(false);
    }
  };

  const back = (
    <Link className={styles.back} to={ARTIFACT_REPOS_PATH}>
      <ArrowLeft size={14} aria-hidden="true" />
      Repositories
    </Link>
  );

  if (missing)
    return (
      <div className={styles.page} data-testid="artifact-repo-page">
        <div className={styles.wrap}>
          {back}
          <p className={styles.note} data-testid="repo-missing">
            There is no such repository. It may have been deleted.
          </p>
        </div>
      </div>
    );

  return (
    <div className={styles.page} data-testid="artifact-repo-page">
      <div className={styles.wrap}>
        {back}
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
        {!repo || !page ? (
          !error && <Loader kind="acquire" label="Acquiring the repository" testId="repo-loading" />
        ) : (
          <>
            <div className={styles.dtop}>
              <div className={styles.dtitle}>
                <div className={styles.tagRow}>
                  <span className={styles.vis}>Hosted here</span>
                  <span className={styles.meta}>{itemsLabel(repo.items)}</span>
                </div>
                <h1 className={styles.h2}>{repo.name}</h1>
                {repo.description && <p className={styles.whyLine}>{repo.description}</p>}
              </div>
              <div className={styles.dacts}>
                <button
                  type="button"
                  className={styles.primary}
                  data-testid="repo-add"
                  onClick={(e) => setAdding({ returnTo: e.currentTarget })}
                >
                  <Plus size={14} aria-hidden="true" />
                  Add an artifact…
                </button>
                <button
                  type="button"
                  className={styles.dangerBtn}
                  data-testid="repo-delete"
                  onClick={(e) => setConfirm({ kind: "delete", returnTo: e.currentTarget })}
                >
                  <Trash2 size={14} aria-hidden="true" />
                  Delete repository…
                </button>
              </div>
            </div>
            <div className={styles.storage} data-testid="repo-storage">
              <span className={styles.lbl}>Storage</span>
              <StoreGauge
                bytes={repo.bytes}
                total={repo.quota_bytes}
                label="Repository storage used"
              />
              <span className={styles.storageText}>
                {storageLabel(repo.bytes, repo.quota_bytes)}
              </span>
            </div>
            {items.length === 0 ? (
              <Panel
                faceClassName={styles.face}
                tone="quiet"
                rail="none"
                className={styles.empty}
                data-testid="repo-empty"
              >
                <Callsign title={repo.name} value="no items" />
                <p className={styles.note}>
                  Nothing in this repository yet. <b>Add an artifact…</b> adds one of yours, or
                  ask an agent to publish with <code className={styles.mono}>repository: “{repo.name}”</code>.
                </p>
              </Panel>
            ) : (
              <div className={styles.list} role="list" aria-label={`Items in ${repo.name}`}>
                <div className={`${styles.lhead} ${styles.itemGrid}`} aria-hidden="true">
                  <span>Kind</span>
                  <span>Item</span>
                  <span>Added by</span>
                  <span>Size</span>
                  <span>Rev</span>
                  <span />
                </div>
                {items.map((i) => {
                  const by = addedByLabel(
                    i.added_by,
                    i.added_by.kind === "session" ? names.session(i.added_by.session_key) : null,
                  );
                  return (
                    <div
                      key={i.id}
                      className={`${styles.row} ${styles.itemGrid}`}
                      role="listitem"
                      data-item={i.id}
                    >
                      <div className={styles.kindCol}>
                        <KindTag kind={i.kind} entry={i.entry} />
                      </div>
                      <div className={styles.main}>
                        <div className={styles.mtop}>
                          <KindTag kind={i.kind} entry={i.entry} />
                          <span className={styles.meta}>
                            <b>{revLabel(i.revision)}</b>
                            <span className={styles.dot}>{formatSize(i.bytes)}</span>
                            <span className={styles.dot}>{ago(i.updated_at)}</span>
                          </span>
                        </div>
                        <span className={styles.title} title={i.title}>
                          {i.title}
                        </span>
                        {i.why && (
                          <p className={styles.why} title={i.why}>
                            {i.why}
                          </p>
                        )}
                      </div>
                      <div className={styles.prov}>
                        <span className={styles.provTop}>
                          <b className={styles.byWho}>{by.who}</b>
                          <span className={styles.ell} title={by.how}>
                            · {by.how}
                          </span>
                        </span>
                      </div>
                      <div className={styles.num}>{formatSize(i.bytes)}</div>
                      <div className={styles.num}>
                        {revLabel(i.revision)}
                        <small>{ago(i.updated_at)}</small>
                      </div>
                      <div className={styles.acts}>
                        <a
                          className={styles.rowBtn}
                          href={appUrl(hostedRepoItemPath(i.id))}
                          target="_blank"
                          rel="noopener noreferrer"
                          aria-label={`Open ${i.title} in a new tab`}
                        >
                          <ExternalLink size={14} aria-hidden="true" />
                          Open
                        </a>
                        <button
                          type="button"
                          className={styles.rowDanger}
                          aria-label={`Remove ${i.title} from ${repo.name}`}
                          onClick={(e) =>
                            setConfirm({ kind: "remove", item: i, returnTo: e.currentTarget })
                          }
                        >
                          Remove
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
            {page.next_cursor && (
              <div className={styles.more}>
                <button
                  type="button"
                  className={styles.ghost}
                  disabled={loadingMore}
                  onClick={() => void loadMore()}
                >
                  {loadingMore ? "Loading…" : `Show more (${page.total - items.length} left)`}
                </button>
              </div>
            )}
          </>
        )}
        <p className={styles.status} role="status">
          {status}
        </p>
      </div>
      {adding && repo && (
        <AddArtifactDialog
          repo={repo}
          items={items}
          returnFocusTo={adding.returnTo}
          onClose={() => setAdding(null)}
          onAdded={(item) => {
            setAdding(null);
            setStatus(`“${item.title}” is in ${repo.name} as ${revLabel(item.revision)}.`);
            void load();
          }}
        />
      )}
      {confirm && repo && (
        <ConfirmDialog
          tag={confirm.kind === "remove" ? "Remove item" : "Delete repository"}
          title={
            confirm.kind === "remove"
              ? `Remove “${confirm.item.title}” from ${repo.name}?`
              : `Delete the repository “${repo.name}”?`
          }
          confirmLabel={confirm.kind === "remove" ? "Remove" : "Delete"}
          danger
          busy={busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => void act()}
          returnFocusTo={confirm.returnTo}
          fallbackFocusSelector="[data-testid='artifact-repo-page'] h1"
          markers={{ dialog: { "data-testid": "repo-confirm" } }}
        >
          <p className={styles.note}>
            {confirm.kind === "remove"
              ? "Every revision of this item leaves the repository. Your own artifact, if it still exists, is not touched."
              : `Every item in it is removed (${itemsLabel(repo.items)}). Your own artifacts are not touched.`}
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}
