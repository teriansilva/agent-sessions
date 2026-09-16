/** One details surface, shared by the wide column and the narrow Details view (#944).
 * Disclosure state lasts for this app visit; children stay mounted so closing a section
 * does not discard objective edits or restart its requests. */
import { useId, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { useSectionState } from "../../app/sectionState";
import styles from "./mission.module.css";

export function MissionDetails({
  missionId,
  context,
  objectives,
  followThrough,
  timeline,
  summaries,
  revealObjectives = 0,
  revealContext = 0,
}: {
  missionId: string;
  context: ReactNode;
  objectives: ReactNode;
  followThrough: ReactNode;
  timeline: ReactNode;
  summaries: {
    context: string;
    objectives: string;
    followThrough: string;
    timeline: string;
  };
  revealObjectives?: number;
  /** #983 P3: bumped when an AI draft is opened in the Context section's composer. */
  revealContext?: number;
}) {
  const [expanded, setExpanded] = useSectionState<Record<string, boolean>>(
    `mission.${missionId}.details`,
    { context: true, objectives: true, followThrough: false, timeline: false },
  );
  const [seenReveal, setSeenReveal] = useSectionState(
    `mission.${missionId}.reveal`,
    0,
  );
  if (seenReveal !== revealObjectives) {
    setSeenReveal(revealObjectives);
    if (revealObjectives)
      setExpanded((prev) => ({ ...prev, objectives: true }));
  }
  const [seenContext, setSeenContext] = useSectionState(
    `mission.${missionId}.revealContext`,
    0,
  );
  if (seenContext !== revealContext) {
    setSeenContext(revealContext);
    if (revealContext) setExpanded((prev) => ({ ...prev, context: true }));
  }
  const id = useId();
  const sections = [
    { key: "context", label: "Context", content: context },
    { key: "objectives", label: "Objectives", content: objectives },
    { key: "followThrough", label: "Follow-through", content: followThrough },
    { key: "timeline", label: "Timeline", content: timeline },
  ] as const;
  return (
    <aside
      className={styles.detailsColumn}
      data-testid="mission-details"
      aria-label="Mission details"
    >
      {sections.map((s) => (
        <section key={s.key} className={styles.disclosure}>
          <button
            type="button"
            data-testid={`detail-${s.key}`}
            className={styles.disclosureToggle}
            aria-expanded={!!expanded[s.key]}
            aria-controls={`${id}-${s.key}`}
            id={`${id}-${s.key}-label`}
            onClick={() =>
              setExpanded((prev) => ({ ...prev, [s.key]: !prev[s.key] }))
            }
          >
            <ChevronRight size={16} aria-hidden="true" />
            <span>{s.label}</span>
            <small>{summaries[s.key]}</small>
          </button>
          <div
            className={styles.disclosureContent}
            id={`${id}-${s.key}`}
            role="region"
            aria-labelledby={`${id}-${s.key}-label`}
            hidden={!expanded[s.key]}
          >
            {s.content}
          </div>
        </section>
      ))}
    </aside>
  );
}
