/** One mission plan in the Library (#1924): its facts, its provenance, and Open / Export / Archive
 *  (or Unarchive). Archiving hides it from new use and never deletes it. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../../lib/api";
import { missionPlanPath } from "../../lib/routes";
import type { MissionPlanRow } from "../../types/missionPlans";
import buttons from "../ui/actionButton.module.css";
import pb from "../playbooks/playbooks.module.css";
import { countsText } from "./planRules";
import styles from "./missionPlans.module.css";

export function MissionPlanCard({
  plan,
  ordinal,
  onChanged,
  onExport,
}: {
  plan: MissionPlanRow;
  ordinal: number;
  onChanged: () => void;
  /** Opens the share dialog's export, this plan's group. */
  onExport: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const archive = async (archived: boolean) => {
    setBusy(true);
    setError("");
    try {
      await api.archiveMissionPlan(plan.id, archived, plan.revision);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn’t change it.");
      // A lost fence (409) means the card is stale: re-read, so the next click is current.
      if (e instanceof ApiError && e.status === 409) onChanged();
    } finally {
      setBusy(false);
    }
  };
  return (
    <article className={pb.card} data-testid={`mission-plan-card-${plan.id}`}>
      <div className={pb.dh} aria-hidden="true">
        <span>Mission plan</span>
        <span className={pb.did}>MP-{String(ordinal).padStart(2, "0")}</span>
      </div>
      <h2>
        <Link to={missionPlanPath(plan.id)} className={styles.title}>
          {plan.title}
        </Link>
      </h2>
      {plan.summary && <p>{plan.summary}</p>}
      <div className={styles.facts}>{countsText(plan.counts)}</div>
      <div className={styles.facts}>
        <span className={styles.stamp}>v{plan.version}</span>
        {plan.origin_kind === "file" ? (
          <span className={`${styles.stamp} ${styles.fromFile}`} data-testid="imported-from-file">
            Imported from a file
          </span>
        ) : (
          <span className={styles.stamp}>Saved from a mission</span>
        )}
        {plan.archived && <span className={`${styles.stamp} ${styles.archivedStamp}`}>Archived</span>}
      </div>
      <div className={pb.actions + " " + styles.cardActions}>
        <Link className={buttons.primary} to={missionPlanPath(plan.id)}>
          Open
        </Link>
        {plan.archived ? (
          <button type="button" className={buttons.ghost} disabled={busy} onClick={() => void archive(false)}>
            Unarchive
          </button>
        ) : (
          <>
            <button type="button" className={buttons.ghost} onClick={onExport}>
              Export
            </button>
            <button type="button" className={buttons.ghost} disabled={busy} onClick={() => void archive(true)}>
              Archive
            </button>
          </>
        )}
      </div>
      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
    </article>
  );
}
