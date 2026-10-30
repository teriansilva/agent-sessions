/** One specialist in the pool (#1693): its job, grade, what it runs on, its skills and how many
 *  instances are running against its capacity. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { engineColor, engineName } from "../../app/engineRoster";
import { api } from "../../lib/api";
import { specialistPath } from "../../lib/routes";
import type { Specialist } from "../../types/specialists";
import { ImportedMark } from "../share/ShareDialogs";
import { GRADE_LABEL } from "./grades";
import buttons from "../ui/actionButton.module.css";
import pb from "../playbooks/playbooks.module.css";
import styles from "./specialists.module.css";

export function SpecialistCard({
  specialist,
  ordinal,
  onChanged,
  imported = false,
}: {
  specialist: Specialist;
  ordinal?: number;
  /** Created by an import from a file (#1714 P2): the Library's provenance mark. */
  imported?: boolean;
  /** After a held slot is released, so the pool reloads. */
  onChanged?: () => void;
}) {
  const [releasing, setReleasing] = useState<string | null>(null);
  const [error, setError] = useState("");
  const releaseSlot = async (rid: string, rev: number) => {
    setReleasing(rid);
    setError("");
    try {
      await api.releaseSpecialistSlot(rid, rev);
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn’t release that slot.");
    } finally {
      setReleasing(null);
    }
  };
  const v = specialist.current;
  const missing = new Set(specialist.skills_missing);
  const outdated = new Set(specialist.skills_outdated);
  // Every version's instances count against the current capacity; lowering it stops nothing.
  const over = specialist.live > v.capacity;
  return (
    <article className={pb.card} data-testid={`specialist-card-${specialist.id}`}>
      <div className={pb.dh} aria-hidden="true">
        Specialist
        {ordinal !== undefined && (
          <span className={pb.did}>S-{String(ordinal).padStart(2, "0")}</span>
        )}
      </div>
      <h2>
        <Link to={specialistPath(specialist.id)} className={styles.title}>
          {v.title}
        </Link>
      </h2>
      <div className={styles.facts}>
        <span
          className={specialist.grade ? styles.grade : styles.ungraded}
          data-testid="specialist-grade"
        >
          {specialist.grade ? GRADE_LABEL[specialist.grade] : "Ungraded"}
        </span>
        <span className={styles.version}>v{v.version}</span>
        {specialist.archived && <span className={styles.version}>archived</span>}
      </div>
      {imported && <ImportedMark />}
      {v.summary && <p>{v.summary}</p>}
      <p className={styles.runsOn} style={{ borderLeftColor: engineColor(v.engine) }}>
        {engineName(v.engine)} · <code>{v.model}</code>
        {v.effort ? <> · effort <code>{v.effort}</code></> : null}
      </p>
      <ul className={styles.skills} aria-label="Skills">
        {v.skills.length === 0 && <li className={styles.none}>No skills</li>}
        {v.skills.map((s) => (
          <li
            key={s.id}
            className={missing.has(s.id) || outdated.has(s.id) ? styles.skillMissing : styles.skill}
          >
            <code>{s.id}</code>
            {missing.has(s.id) && <span> · missing from your library</span>}
            {outdated.has(s.id) && <span> · changed in your library: save a new version</span>}
          </li>
        ))}
      </ul>
      <p
        className={over ? styles.overCapacity : styles.capacity}
        data-testid="specialist-capacity"
      >
        <b>{specialist.live}</b> / {v.capacity} running
        {over && " · over capacity: no new instances until one ends"}
      </p>
      {(specialist.held ?? []).map((h) => (
        <div key={h.reservation_id} className={styles.held} data-testid="specialist-held">
          <span>
            1 slot held: {h.reason}. Its session couldn’t be proven finished.
          </span>
          <button
            type="button"
            className={buttons.ghost}
            disabled={releasing !== null}
            onClick={() => void releaseSlot(h.reservation_id, h.rev)}
          >
            Release
          </button>
        </div>
      ))}
      {error && (
        <p role="alert" className={styles.refused}>
          {error}
        </p>
      )}
    </article>
  );
}
