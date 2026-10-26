/** One specialist in the pool (#1693): its job, grade, what it runs on, its skills and how many
 *  instances are running against its capacity. */
import { Link } from "react-router-dom";
import { engineColor, engineName } from "../../app/engineRoster";
import { specialistPath } from "../../lib/routes";
import type { Specialist } from "../../types/specialists";
import { GRADE_LABEL } from "./grades";
import pb from "../playbooks/playbooks.module.css";
import styles from "./specialists.module.css";

export function SpecialistCard({
  specialist,
  ordinal,
}: {
  specialist: Specialist;
  ordinal?: number;
}) {
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
    </article>
  );
}
