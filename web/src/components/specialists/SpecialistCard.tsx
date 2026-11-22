/** One specialist in the pool (#1693) on the shared Library card (#2144): its job, what it runs on
 *  and its grade, how many instances run against its capacity, and its skills. A tap anywhere
 *  opens it (today's editor). A held slot keeps its line and its own Release, which acts without
 *  opening the card. */
import { useState } from "react";
import { engineColor, engineName } from "../../app/engineRoster";
import { api } from "../../lib/api";
import { specialistPath } from "../../lib/routes";
import type { Specialist } from "../../types/specialists";
import { ImportedMark } from "../share/ShareDialogs";
import { CardExport, Count, Dot, LibraryCard } from "../library/LibraryCard";
import lib from "../library/library.module.css";
import { GRADE_LABEL } from "./grades";
import buttons from "../ui/actionButton.module.css";

export function SpecialistCard({
  specialist,
  onChanged,
  imported = false,
}: {
  specialist: Specialist;
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
  const problems = specialist.skills_missing.length + specialist.skills_outdated.length;
  // Every version's instances count against the current capacity; lowering it stops nothing.
  const over = specialist.live > v.capacity;
  return (
    <LibraryCard
      testId={`specialist-card-${specialist.id}`}
      to={specialistPath(specialist.id)}
      title={v.title}
      kind="Specialist"
      version={String(v.version)}
      badges={specialist.archived ? <span className={lib.grade}>Archived</span> : undefined}
      source={imported ? "imported" : "local"}
      summary={v.summary || "No summary."}
      meta={[
        <span
          key="runs"
          className={lib.runs}
          style={{ borderLeftColor: engineColor(v.engine) }}
        >
          {engineName(v.engine)} <Dot /> <code>{v.model}</code>
          {v.effort ? (
            <>
              <Dot /> effort <code>{v.effort}</code>
            </>
          ) : null}
          <Dot />
          <span
            className={`${lib.grade} ${specialist.grade ? "" : lib.ungraded}`.trim()}
            data-testid="specialist-grade"
          >
            {specialist.grade ? GRADE_LABEL[specialist.grade] : "Ungraded"}
          </span>
        </span>,
        <>
          <span data-testid="specialist-capacity" className={over ? lib.over : undefined}>
            <b>{specialist.live}</b> / {v.capacity} running
            {over && " · over capacity: no new instances until one ends"}
          </span>
          <Dot />
          <Count n={v.skills.length} one="skill" />
          {problems > 0 && (
            <>
              <Dot />
              <span className={lib.over} data-testid="specialist-skill-problems">
                {specialist.skills_missing.length > 0 &&
                  `${specialist.skills_missing.join(", ")} missing from your library`}
                {specialist.skills_missing.length > 0 && specialist.skills_outdated.length > 0 && " · "}
                {specialist.skills_outdated.length > 0 &&
                  `${specialist.skills_outdated.join(", ")} changed in your library: save a new version`}
              </span>
            </>
          )}
        </>,
      ]}
      provenance={imported ? <ImportedMark /> : "Unsigned · made on this host"}
      actions={<CardExport kind="specialist" id={specialist.id} title={v.title} />}
    >
      {(specialist.held ?? []).map((h) => (
        <div key={h.reservation_id} className={lib.held} data-testid="specialist-held">
          <span>1 slot held: {h.reason}. Its session couldn’t be proven finished.</span>
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
        <p role="alert" className={lib.error}>
          {error}
        </p>
      )}
    </LibraryCard>
  );
}
