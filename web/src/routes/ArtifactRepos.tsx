/** Library → Artifacts → REPOSITORIES (#2347 P1, mockup frames 1 and 6): the shared artifact
 *  repositories hosted on this BattleLab, each with its item count and its storage against its
 *  quota, and New repository. Repositories from paired instances are a later phase; this view lists
 *  only the ones hosted here. */
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Plus } from "lucide-react";

import { ApiError, api } from "../lib/api";
import { artifactRepoPath } from "../lib/routes";
import type { ArtifactRepoList } from "../types/artifacts";
import { Callsign, Panel } from "../components/hud/Panel";
import { Loader } from "../components/hud/Loader";
import { ArtifactsViewTabs } from "../components/artifacts/ArtifactsViewTabs";
import { NewRepoDialog } from "../components/artifacts/RepoDialogs";
import { repoFacts } from "../components/artifacts/repoModel";
import styles from "../components/artifacts/artifacts.module.css";

const message = (e: unknown) =>
  e instanceof ApiError || e instanceof Error
    ? e.message
    : "The request failed. Try again.";

export default function ArtifactRepos() {
  const navigate = useNavigate();
  const [list, setList] = useState<ArtifactRepoList | null>(null);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState<{ returnTo: HTMLElement | null } | null>(null);

  const load = useCallback(
    () =>
      api.artifactRepos().then(
        (next) => {
          setList(next);
          setError("");
        },
        (e: unknown) => setError(message(e)),
      ),
    [],
  );
  useEffect(() => {
    void load();
  }, [load]);

  const repos = list?.repositories ?? [];
  const openNew = (e: { currentTarget: HTMLElement }) =>
    setCreating({ returnTo: e.currentTarget });
  const newButton = (
    <button
      type="button"
      className={styles.primary}
      onClick={openNew}
      data-testid="repo-new"
    >
      <Plus size={14} aria-hidden="true" />
      New repository
    </button>
  );

  return (
    <div className={styles.page} data-testid="artifact-repos-page">
      <div className={styles.wrap}>
        <div>
          <p className={styles.kicker}>
            <span className="nj-notch" aria-hidden="true" />
            Library <span aria-hidden="true">//</span> <b>Artifacts</b>
          </p>
          <h1 className={styles.h1}>Artifacts</h1>
        </div>
        <ArtifactsViewTabs value="repos" />
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
        {list === null && !error ? (
          <Loader kind="acquire" label="Acquiring repositories" testId="repos-loading" />
        ) : list && repos.length === 0 ? (
          <Panel
            faceClassName={styles.face}
            tone="quiet"
            rail="none"
            className={styles.empty}
            data-testid="repos-none"
          >
            <Callsign title="Repositories" value="none yet" />
            <h3>Nothing yet</h3>
            <p className={styles.note}>
              No repositories here yet. Create one to keep files that several BattleLabs use:
              add your artifacts to it, or let an agent publish into it by name.
            </p>
            <div className={styles.btnRow}>{newButton}</div>
          </Panel>
        ) : list ? (
          <>
            <div className={styles.headRow}>
              <p className={styles.lede}>
                Named collections of artifacts hosted on this BattleLab. Each item is a frozen
                revision; adding an artifact copies no bytes.
              </p>
              {newButton}
            </div>
            <h2 className={styles.sh2}>Hosted on this BattleLab</h2>
            <ul className={styles.repoList} aria-label="Repositories hosted here">
              {repos.map((r) => (
                <li key={r.id} className={styles.repoRow} data-repo={r.id}>
                  <Link className={styles.repoName} to={artifactRepoPath(r.id)}>
                    {r.name}
                  </Link>
                  <span className={styles.vis}>Host</span>
                  <span className={styles.repoFacts}>{repoFacts(r)}</span>
                  {r.description && <p className={styles.why}>{r.description}</p>}
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </div>
      {creating && (
        <NewRepoDialog
          returnFocusTo={creating.returnTo}
          onClose={() => setCreating(null)}
          onCreated={(repo) => {
            setCreating(null);
            navigate(artifactRepoPath(repo.id));
          }}
        />
      )}
    </div>
  );
}
