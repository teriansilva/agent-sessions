import { Link } from "react-router-dom";
import {
  engineColor,
  engineName,
  offeredModels,
  useEngineRoster,
} from "../../app/engineRoster";
import { playbookPath } from "../../lib/routes";
import type { PlaybookCardData, PlaybookStep } from "../../types/playbooks";
import { bindingRole } from "../../types/playbooks";
import { ImportedMark } from "../share/ShareDialogs";
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

export function PlaybookSource({ card }: { card: PlaybookCardData }) {
  return (
    <div className={styles.tags}>
      <span className={styles.badge}>{card.source}</span>
      <span>
        {card.source === "local"
          ? "Unsigned"
          : card.source === "bundled"
            ? "Shipped with this release"
            : "Catalog · trust not reported"}
      </span>
      {card.version && <span className={styles.badge}>v{card.version}</span>}
      {card.default && <strong className={styles.defaultTag}>Default</strong>}
    </div>
  );
}

/** Shared with the New project wizard: a card displays the stored references, never assigns. */
export function PlaybookCard({
  card,
  onSelect,
  ordinal,
  imported = false,
}: {
  card: PlaybookCardData;
  onSelect?: (card: PlaybookCardData) => void;
  /** A local copy created from a share bundle (#1714): it carries the mark. */
  imported?: boolean;
  /** The card's place in the library list, for the dossier's stamp line (#1574 board B1). */
  ordinal?: number;
}) {
  return (
    <article
      className={`${styles.card} ${card.ok ? "" : styles.invalid}`}
      data-testid={`playbook-card-${card.id}`}
    >
      <div className={styles.dh} aria-hidden="true">
        Playbook
        {ordinal !== undefined && (
          <span className={styles.did}>P-{String(ordinal).padStart(2, "0")}</span>
        )}
      </div>
      <h2>{card.name || card.id}</h2>
      <PlaybookSource card={card} />
      {imported && <ImportedMark />}
      {card.ok ? (
        <>
          <p>{card.summary}</p>
          {(card.flows ?? []).map((flow) => (
            <section
              className={styles.flow}
              key={flow.id}
              aria-label={flow.title}
            >
              <div className={styles.kicker}>
                {flow.title} // {flow.steps.length} steps
              </div>
              <ol>
                {flow.steps.map((step) => (
                  <li key={step.id}>
                    <span>
                      {step.title}
                      {step.note ? " · note" : ""}
                    </span>
                    <PlaybookActor step={step} />
                  </li>
                ))}
              </ol>
            </section>
          ))}
          {!card.flows?.length && <p>No flow defined.</p>}
          {card.ships && (
            <p className={styles.counts}>
              {card.ships.materials} files · {card.ships.runbooks} runbooks ·{" "}
              {card.ships.templates} templates · {card.ships.variables}{" "}
              variables
            </p>
          )}
          <p>
            Requires: {card.requires?.binaries.join(", ") || "no binaries"}
            {card.connections?.length
              ? ` · ${card.connections.join(", ")} connection`
              : ""}
          </p>
        </>
      ) : (
        <div role="note">
          <strong>This playbook could not be loaded</strong>
          <p>{card.error}</p>
          <p>Other playbooks remain available.</p>
        </div>
      )}
      <div className={styles.actions}>
        {onSelect ? (
          <button
            className={buttons.ghost}
            disabled={!card.ok}
            onClick={() => onSelect(card)}
          >
            Select
          </button>
        ) : card.ok ? (
          <Link className={buttons.ghost} to={playbookPath(card.id)}>
            Open
          </Link>
        ) : (
          <span>Unavailable</span>
        )}
      </div>
    </article>
  );
}
