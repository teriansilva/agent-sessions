import { Check, GitMerge, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "../../lib/api";
import { announceClustersChanged, CLUSTERS_CHANGED_EVENT } from "../../lib/clusterEvents";
import { CLUSTER_TITLE_MAX } from "../../lib/clusters";
import type { ClusterDetail, SessionClusterFacet } from "../../types/api";
import { ClusterPickerModal } from "./ClusterPickerModal";
import { RowMenu, type RowMenuEntry } from "./RowMenu";
import styles from "./Clusters.module.css";

interface Props {
  /** The selected cluster, from the list's facets — or `undefined` when it is no longer there. */
  cluster: SessionClusterFacet | undefined;
  clusterId: string;
  projectName: string;
  /** Whether the AI endpoint is set (re-cluster needs it). */
  configured: boolean;
  /** Point the list somewhere else: after a merge (the target) or a dissolve (the project). */
  onSelect: (clusterId: string) => void;
}

/** The selected cluster's head, above the list (#1997): its title, whether HQ or the operator
 *  named it, its summary, and what the operator can do with it. Every write goes through the
 *  cluster routes and then announces the change, so the list, the tree and the row chips re-read
 *  from the server instead of being patched here. */
export function ClusterHead({ cluster, clusterId, projectName, configured, onSelect }: Props) {
  const [detail, setDetail] = useState<ClusterDetail | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [merging, setMerging] = useState(false);
  const [mergeReturnFocus, setMergeReturnFocus] = useState<HTMLElement | null>(null);
  const projectId = cluster?.project_id;

  // The summary and the source are not on the facet: read them, and again whenever clusters
  // change in this tab. A failed read leaves the head with what the facet already says.
  const load = useCallback(() => {
    if (!projectId) return () => {};
    let alive = true;
    api
      .clusters(projectId)
      .then((r) => {
        if (alive) setDetail(r.clusters.find((c) => c.id === clusterId) ?? null);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [projectId, clusterId]);
  useEffect(() => load(), [load, cluster?.title, cluster?.kept]);
  useEffect(() => {
    const onChange = () => void load();
    window.addEventListener(CLUSTERS_CHANGED_EVENT, onChange);
    return () => window.removeEventListener(CLUSTERS_CHANGED_EVENT, onChange);
  }, [load]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      announceClustersChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "That didn’t work. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  if (!cluster) {
    return (
      <div className={styles.head} data-testid="cluster-head">
        <p className={styles.note} role="status">
          This cluster is no longer there. It was dissolved, merged, or its sessions moved on.
        </p>
        <div className={styles.headActions}>
          <button type="button" className={styles.btn} onClick={() => onSelect("")}>
            Show the project
          </button>
        </div>
      </div>
    );
  }

  const summary = detail?.id === clusterId ? detail.summary : "";
  const commitRename = () => {
    const title = draft.trim();
    if (!title || title === cluster.title) {
      setEditing(false);
      return;
    }
    void run(async () => {
      await api.patchCluster(clusterId, { title });
      setEditing(false);
    });
  };

  const menu: RowMenuEntry[] = [
    {
      key: "merge",
      label: "Merge into…",
      ariaLabel: "Merge this cluster into another",
      icon: <GitMerge size={15} />,
      disabled: busy,
      onSelect: () => {
        setMergeReturnFocus(document.activeElement as HTMLElement | null);
        setMerging(true);
      },
    },
    {
      key: "recluster",
      label: `Re-cluster ${projectName} now`,
      ariaLabel: `Re-cluster ${projectName} now`,
      icon: <RefreshCw size={15} />,
      disabled: busy || !configured,
      onSelect: () =>
        void run(async () => {
          const r = await api.recluster(cluster.project_id);
          const mine = r.projects.find((p) => p.project_id === cluster.project_id);
          setNotice(
            mine?.status === "error"
              ? `HQ could not re-cluster: ${mine.error ?? "the AI endpoint failed"}.`
              : "HQ looked at this project again.",
          );
        }),
    },
    "separator",
    {
      key: "dissolve",
      label: "Dissolve cluster",
      ariaLabel: "Dissolve this cluster",
      icon: <Trash2 size={15} />,
      danger: true,
      disabled: busy,
      onSelect: () =>
        void run(async () => {
          await api.dissolveCluster(clusterId);
          onSelect("");
        }),
    },
  ];

  return (
    <div className={styles.head} data-testid="cluster-head">
      <div className={styles.headKicker}>
        <span>Cluster · {projectName}</span>
        <span className={cluster.kept ? styles.tagKept : styles.tag} data-testid="cluster-head-tag">
          {cluster.kept ? "Kept · your title" : "AI title"}
        </span>
      </div>
      {editing ? (
        <form
          className={styles.rename}
          onSubmit={(e) => {
            e.preventDefault();
            commitRename();
          }}
        >
          <input
            ref={(el) => el?.focus()}
            className={styles.renameInput}
            aria-label="Cluster title"
            maxLength={CLUSTER_TITLE_MAX}
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") setEditing(false);
            }}
          />
          <button type="submit" className={styles.iconBtn} aria-label="Save cluster title" disabled={busy}>
            <Check size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            aria-label="Cancel cluster rename"
            disabled={busy}
            onClick={() => setEditing(false)}
          >
            <X size={15} aria-hidden="true" />
          </button>
        </form>
      ) : (
        <h3 className={styles.headTitle} data-testid="cluster-head-title">
          {cluster.title}
        </h3>
      )}
      {summary ? <p className={styles.headSummary}>{summary}</p> : null}
      {cluster.kept && !editing ? (
        <p className={styles.headSummary}>
          HQ still moves new sessions in and out. It no longer changes the title.
        </p>
      ) : null}
      {error ? (
        <p className={styles.err} role="alert">
          {error}
        </p>
      ) : null}
      <p className={styles.note} role="status">
        {notice}
      </p>
      {!editing ? (
        <div className={styles.headActions}>
          <button
            type="button"
            className={styles.btn}
            disabled={busy}
            onClick={() => {
              setDraft(cluster.title);
              setEditing(true);
            }}
          >
            Rename
          </button>
          <button
            type="button"
            className={styles.btn}
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await api.patchCluster(clusterId, { keep: !cluster.kept });
              })
            }
          >
            {cluster.kept ? "Let HQ rename" : "Keep title"}
          </button>
          <RowMenu
            items={menu}
            title={cluster.title}
            sheetTitle="Cluster actions"
            triggerLabel="Cluster actions"
            triggerClassName={styles.iconBtn}
            triggerTestId="cluster-head-menu"
          />
        </div>
      ) : null}
      {merging ? (
        <ClusterPickerModal
          heading="Merge into"
          subjectLabel="CLUSTER"
          subject={cluster.title}
          projectId={cluster.project_id}
          excludeId={clusterId}
          busy={busy}
          error={error}
          onCancel={() => setMerging(false)}
          onPick={(target) =>
            void run(async () => {
              await api.mergeCluster(clusterId, target.id);
              setMerging(false);
              onSelect(target.id);
            })
          }
          returnFocusTo={mergeReturnFocus}
        />
      ) : null}
    </div>
  );
}
