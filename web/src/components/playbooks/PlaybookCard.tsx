import { Link } from "react-router-dom";
import { Plus } from "lucide-react";
import {
  engineColor,
  engineName,
  offeredModels,
  useEngineRoster,
} from "../../app/engineRoster";
import { PLAYBOOK_NEW_PATH, playbookPath } from "../../lib/routes";
import type { PlaybookCardData, PlaybookStep } from "../../types/playbooks";
import {
  CardExport,
  Count,
  DefaultMark,
  Dot,
  LibraryCard,
  type LibrarySource,
} from "../library/LibraryCard";
import lib from "../library/library.module.css";
import { bindingRole } from "../../types/playbooks";
import { playbookProvenance } from "./playbookSource";
import buttons from "../ui/actionButton.module.css";
import styles from "./playbooks.module.css";

export function PlaybookActor({ step }: { step: PlaybookStep }) {
  const roster = useEngineRoster();
  const actor = step.actor;
  if (actor.kind !== "agent")
    return (
      <span className={styles.badge}>
        {actor.kind === "external"
          ? actor.label || "External"
          : actor.kind === "none"
            ? "No actor"
            : "Operator"}
      </span>
    );
  const role = bindingRole(step);
  if (role) {
    // Format 5 (#1654): a role, filled per mission from the pool. Never an engine or model.
    const grade =
      { fast: "Intern", strong: "Specialist", frontier: "Senior" }[
        role.min_grade ?? ""
      ] ?? "Any grade";
    return (
      <span className={styles.role}>
        <span className={styles.roleGrade}>{grade}</span>
        {role.skills.length ? (
          role.skills.map((s) => <code key={s}>{s}</code>)
        ) : (
          <span>no skills required</span>
        )}
      </span>
    );
  }
  const id = actor.engine ?? "";
  const engine = roster.engines.find((entry) => entry.id === id);
  const reason =
    roster.status !== "ready"
      ? "Roster unavailable"
      : !engine || !engine.present
        ? "Unresolved: agent absent"
        : engine.status === "retiring"
          ? "Unresolved: agent retiring"
          : actor.model &&
              actor.model !== "default" &&
              !offeredModels(id).some(
                (m) => m.id === actor.model || m.aliases.includes(actor.model!),
              )
            ? "Unresolved: model unavailable"
            : "";
  return (
    <span className={styles.actor} style={{ borderLeftColor: engineColor(id) }}>
      {id ? engineName(id) : "No stored engine"}{" "}
      <code>{actor.model || "default"}</code>
      {reason && <span className={styles.warn}>{reason}</span>}
    </span>
  );
}

/** The one primary both Playbooks tabs' head carries (#2144 board 1). */
export function NewPlaybookLink() {
  return (
    <Link className={buttons.primary} to={PLAYBOOK_NEW_PATH}>
      <Plus size={16} aria-hidden />
      New playbook
    </Link>
  );
}

/** One playbook on the Library grid (#2144): the shared card with the playbook's meta row. A tap
 *  anywhere opens it; Export is offered on local copies only (bundled and catalog playbooks are
 *  not in the share catalog). The New project wizard's radio cards are a different component. */
export function PlaybookCard({
  card,
  source,
}: {
  card: PlaybookCardData;
  source: LibrarySource;
}) {
  const flows = card.flows ?? [];
  const steps = flows.reduce((n, f) => n + f.steps.length, 0);
  const files = card.ships ? card.ships.materials + card.ships.runbooks + card.ships.templates : null;
  const title = card.name || card.id;
  return (
    <LibraryCard
      testId={`playbook-card-${card.id}`}
      to={card.ok ? playbookPath(card.id) : undefined}
      title={title}
      kind="Playbook"
      version={card.version}
      badges={card.default ? <DefaultMark /> : undefined}
      source={source}
      invalid={!card.ok}
      summary={card.ok ? card.summary || "No summary." : undefined}
      meta={
        card.ok ? (
          <>
            <Count n={flows.length} one="flow" />
            <Dot />
            <Count n={steps} one="step" />
            {files !== null && (
              <>
                <Dot />
                <Count n={files} one="file" />
              </>
            )}
          </>
        ) : undefined
      }
      provenance={playbookProvenance(card, source)}
      actions={
        card.ok && card.source === "local" ? (
          <CardExport kind="playbook" id={card.id} title={title} />
        ) : undefined
      }
    >
      {!card.ok && (
        <div role="note" className={lib.note}>
          <strong>This playbook could not be loaded.</strong> {card.error} Other playbooks remain
          available.
        </div>
      )}
    </LibraryCard>
  );
}
