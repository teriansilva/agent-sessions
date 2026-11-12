/** Library → Mission plans (#1924, #1613 P7a): reusable, slot-only, inert plans saved from a
 *  mission or imported from a file. Nothing here plans, dispatches or launches anything. */
import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { MissionPlanCard } from "../components/missionPlans/MissionPlanCard";
import { NEVER_SAVED } from "../components/missionPlans/planRules";
import { usePlaybookRead } from "../components/playbooks/usePlaybooks";
import { ShareActions, ShareExportDialog } from "../components/share/ShareDialogs";
import { ApiError, api } from "../lib/api";
import { MISSION_PLANS_PATH } from "../lib/routes";
import type { MissionPlanDetail } from "../types/missionPlans";
import buttons from "../components/ui/actionButton.module.css";
import pb from "../components/playbooks/playbooks.module.css";
import styles from "../components/missionPlans/missionPlans.module.css";

export default function MissionPlans() {
  const { planId } = useParams();
  return (
    <main className={pb.page} data-testid="mission-plans-page">
      {planId ? <Detail key={planId} id={planId} /> : <List />}
    </main>
  );
}

const INTRO =
  "What a mission achieves and in what order, saved for reuse: its objectives, their order, the project slots they need and the skills each step asks for. A saved plan names no real project and runs nothing by itself.";

function List() {
  const [showArchived, setShowArchived] = useState(false);
  const read = useCallback(() => api.missionPlans(true), []);
  const { data, error, loading, reload } = usePlaybookRead(read);
  const [exporting, setExporting] = useState<string | null>(null);
  const all = data?.plans ?? [];
  const live = all.filter((p) => !p.archived);
  const archived = all.length - live.length;
  // Show archived ADDS the archived plans to the live ones (the mockup review on #1924).
  const rows = showArchived ? all : live;
  return (
    <>
      <div className={`nj-kicker ${pb.kicker}`}>
        <span className="nj-notch" aria-hidden="true" />
        Library // <b>mission plans</b>
        {data ? ` // ${rows.length}` : ""}
      </div>
      <div className={pb.header}>
        <h1>Mission plans</h1>
        <ShareActions kind="mission-plan" onImported={() => void reload()}>
          {archived > 0 && (
            <button
              type="button"
              className={buttons.ghost}
              aria-pressed={showArchived}
              data-testid="mission-plans-show-archived"
              onClick={() => setShowArchived((v) => !v)}
            >
              Show archived ({archived})
            </button>
          )}
        </ShareActions>
      </div>
      <p className={pb.intro}>{INTRO}</p>
      {error && (
        <div className={pb.error} role="alert">
          {error}{" "}
          <button className={buttons.ghost} onClick={() => void reload()}>
            Try again
          </button>
        </div>
      )}
      {loading && !data && <p role="status">Loading mission plans…</p>}
      {data && !live.length && !showArchived && (
        <div className={pb.empty} data-testid="mission-plans-empty">
          <h2>{archived ? "Every mission plan is archived" : "No mission plans yet"}</h2>
          <p>A mission plan comes from a mission that worked, or from a file someone shared with you.</p>
          <ol className={styles.emptyHow}>
            <li>Open a mission that has a plan.</li>
            <li>
              Choose <b>Save as mission plan</b> in its ⋯ menu.
            </li>
            <li>
              Or choose <b>Import…</b> above for a <code>.battlelab</code> file.
            </li>
          </ol>
        </div>
      )}
      <div className={pb.grid}>
        {rows.map((p, i) => (
          <MissionPlanCard
            key={p.id}
            plan={p}
            ordinal={i + 1}
            onChanged={() => void reload()}
            onExport={() => setExporting(`mission-plan:${p.id}`)}
          />
        ))}
      </div>
      {exporting && (
        <ShareExportDialog kind="mission-plan" preselect={exporting} onClose={() => setExporting(null)} />
      )}
    </>
  );
}

function Detail({ id }: { id: string }) {
  const read = useCallback(() => api.missionPlan(id), [id]);
  const { data, error, loading, reload } = usePlaybookRead(read);
  const [exporting, setExporting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const archive = async (plan: MissionPlanDetail) => {
    setBusy(true);
    setActionError("");
    try {
      await api.archiveMissionPlan(plan.id, !plan.archived, plan.revision);
      await reload();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Couldn’t change it.");
      if (e instanceof ApiError && e.status === 409) void reload();
    } finally {
      setBusy(false);
    }
  };
  if (error)
    return (
      <div className={pb.error} role="alert">
        {error} <Link to={MISSION_PLANS_PATH}>Back to mission plans</Link>
      </div>
    );
  if (loading && !data) return <p role="status">Loading the mission plan…</p>;
  if (!data) return null;
  const doc = data.document;
  const slotLabel = new Map(doc.slots.map((s) => [s.id, s.label]));
  return (
    <>
      <div className={`nj-kicker ${pb.kicker}`}>
        <span className="nj-notch" aria-hidden="true" />
        Library // <Link to={MISSION_PLANS_PATH}>mission plans</Link> // <b>{data.id}</b>
      </div>
      <div className={pb.header}>
        <div>
          <h1>{data.title}</h1>
          {data.summary && <p className={pb.intro}>{data.summary}</p>}
          <div className={styles.facts}>
            <span className={styles.stamp}>v{data.version}</span>
            <span className={`${styles.stamp} ${data.origin_kind === "file" ? styles.fromFile : ""}`}>
              {data.origin_kind === "file" ? "Imported from a file" : "Saved from a mission"}
            </span>
            {data.archived && <span className={`${styles.stamp} ${styles.archivedStamp}`}>Archived</span>}
          </div>
        </div>
        <div className={pb.actions}>
          {!data.archived && (
            <button type="button" className={buttons.ghost} onClick={() => setExporting(true)}>
              Export
            </button>
          )}
          <button type="button" className={buttons.ghost} disabled={busy} onClick={() => void archive(data)}>
            {data.archived ? "Unarchive" : "Archive"}
          </button>
        </div>
      </div>
      {actionError && (
        <p role="alert" className={styles.error}>
          {actionError}
        </p>
      )}
      <p className={styles.inert} data-testid="mission-plan-inert">
        <b>This plan runs nothing.</b> It has no projects, agents or values of its own: a mission
        planned from it later fills each slot with one of your projects, and you approve that
        mission before anything starts.
      </p>
      <div className={styles.cols}>
        <div className={styles.side}>
          <section className={styles.panel} aria-label="Project slots">
            <h3 className={styles.panelHead}>Project slots</h3>
            {doc.slots.map((s) => (
              <div className={styles.slot} key={s.id}>
                <span className={styles.slotId}>{s.id}</span>
                <span>{s.label}</span>
              </div>
            ))}
          </section>
          <section className={styles.quiet} aria-label="Never saved in a plan">
            <h3 className={styles.panelHead}>Never saved in a plan</h3>
            <ul className={styles.never}>
              {NEVER_SAVED.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          </section>
        </div>
        <section className={styles.panel} aria-label="Objectives, in order">
          <h3 className={styles.panelHead}>Objectives, in order</h3>
          {doc.objectives.map((o) => (
            <div className={styles.objective} key={o.id} data-testid={`mission-plan-objective-${o.id}`}>
              <div className={styles.objHead}>
                <span className={styles.oid}>{o.id}</span>
                <span className={styles.otitle}>{o.procedure.title}</span>
                <span className={styles.stamp}>
                  {o.slot.toUpperCase()} · {slotLabel.get(o.slot)}
                </span>
                <span className={styles.stamp}>
                  {o.procedure.source === "playbook"
                    ? `playbook · ${o.procedure.playbook_id ?? ""}`
                    : "mission-local procedure"}
                </span>
                {o.after?.length ? <span className={styles.after}>after {o.after.join(", ")}</span> : null}
              </div>
              <ul className={styles.steps}>
                {data.steps
                  .filter((s) => s.objective === o.id)
                  .map((s) => (
                    <li className={styles.step} key={s.id}>
                      <span>{s.title}</span>
                      <span className={styles.actor}>{s.actor}</span>
                      <span className={styles.need}>
                        {s.role?.skills.length ? (
                          s.role.skills.map((k) => (
                            <span className={styles.skill} key={k}>
                              {k}
                            </span>
                          ))
                        ) : (
                          <span className={styles.none}>no skills asked</span>
                        )}
                        {s.role?.min_grade && <span className={styles.grade}>min {s.role.min_grade}</span>}
                      </span>
                    </li>
                  ))}
              </ul>
            </div>
          ))}
        </section>
      </div>
      {exporting && (
        <ShareExportDialog
          kind="mission-plan"
          preselect={`mission-plan:${data.id}`}
          onClose={() => setExporting(false)}
        />
      )}
    </>
  );
}
