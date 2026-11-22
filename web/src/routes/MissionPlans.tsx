/** Library → Mission plans (#1924, #1613 P7a): reusable, slot-only, inert plans saved from a
 *  mission or imported from a file. Nothing here plans, dispatches or launches anything. */
import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { MissionPlanCard } from "../components/missionPlans/MissionPlanCard";
import { NEVER_SAVED } from "../components/missionPlans/planRules";
import { usePlaybookRead } from "../components/playbooks/usePlaybooks";
import { ImportedMark } from "../components/share/ShareDialogs";
import { useExportEntry, useImportEntry } from "../components/share/useLibraryShare";
import { ActionBar, Fact, LibraryDetail, MoreMenu, SourceFact } from "../components/library/LibraryDetail";
import { LibraryGrid, LibraryHead, LibraryToolbar, type Facet } from "../components/library/LibraryGrid";
import lib from "../components/library/library.module.css";
import { Archive, ArchiveRestore, Download } from "lucide-react";
import { ApiError, api } from "../lib/api";
import { MISSION_PLANS_PATH } from "../lib/routes";
import type { MissionPlanDetail } from "../types/missionPlans";
import buttons from "../components/ui/actionButton.module.css";
import pb from "../components/playbooks/playbooks.module.css";
import styles from "../components/missionPlans/missionPlans.module.css";

export default function MissionPlans() {
  const { planId } = useParams();
  return (
    <main className={`${pb.page} ${lib.page}`} data-testid="mission-plans-page">
      {planId ? <Detail key={planId} id={planId} /> : <List />}
    </main>
  );
}

const INTRO =
  "What a mission achieves and in what order, saved for reuse: its objectives, their order, the project slots they need and the skills each step asks for. A saved plan names no real project and runs nothing by itself.";

type Show = "all" | "mission" | "imported" | "archived";

function List() {
  const read = useCallback(() => api.missionPlans(true), []);
  const { data, error, loading, reload } = usePlaybookRead(read);
  const [search, setSearch] = useState("");
  // Archived plans are hidden from new use, never lost: the Archived chip shows them.
  const [show, setShow] = useState<Show>("all");
  const importer = useImportEntry(() => void reload());
  const all = data?.plans ?? [];
  const needle = search.trim().toLowerCase();
  const matching = all.filter((p) =>
    [p.id, p.title, p.summary].join(" ").toLowerCase().includes(needle),
  );
  const live = matching.filter((p) => !p.archived);
  const pick = (v: Show) =>
    // Archived ADDS the archived plans to the live ones, never replacing them (#1924's review).
    v === "archived"
      ? matching
      : v === "all"
        ? live
        : live.filter((p) => (p.origin_kind === "file") === (v === "imported"));
  const rows = pick(show);
  const facets: Facet<Show>[] = (
    [
      { value: "all", label: "All" },
      { value: "mission", label: "Saved from a mission" },
      { value: "imported", label: "Imported" },
      { value: "archived", label: "Archived" },
    ] as { value: Show; label: string }[]
  )
    .map((f) => ({
      ...f,
      n: f.value === "archived" ? matching.filter((p) => p.archived).length : pick(f.value).length,
    }))
    .filter((f) => f.value === "all" || f.value === show || f.n > 0);
  const anyLive = all.some((p) => !p.archived);
  return (
    <>
      {/* No header primary: a plan is saved from a mission (its ⋯ → Save as mission plan). */}
      <LibraryHead
        section="mission plans"
        title="Mission plans"
        count={data ? all.filter((p) => !p.archived).length : undefined}
      />
      <p className={lib.intro}>{INTRO}</p>
      <LibraryToolbar
        label="Search mission plans"
        placeholder="Title or summary…"
        search={search}
        onSearch={setSearch}
        facets={data ? facets : undefined}
        facet={show}
        onFacet={setShow}
        count={data ? `${rows.length} ${rows.length === 1 ? "plan" : "plans"}` : undefined}
      />
      {error && (
        <div className={lib.error} role="alert">
          {error}{" "}
          <button className={buttons.ghost} onClick={() => void reload()}>
            Try again
          </button>
        </div>
      )}
      {loading && !data && <p role="status">Loading mission plans…</p>}
      {data && !anyLive && show === "all" && (
        <div className={lib.empty} data-testid="mission-plans-empty">
          <h2>{all.length ? "Every mission plan is archived" : "No mission plans yet"}</h2>
          <p>A mission plan comes from a mission that worked, or from a file someone shared with you.</p>
          <ol className={styles.emptyHow}>
            <li>Open a mission that has a plan.</li>
            <li>
              Choose <b>Save as mission plan</b> in its ⋯ menu.
            </li>
            <li>
              Or choose <b>Import from file</b> below for a <code>.battlelab</code> file.
            </li>
          </ol>
        </div>
      )}
      {(data || error) && (
        <LibraryGrid
          testId="mission-plan-grid"
          onImport={show === "archived" ? undefined : importer.open}
          importHint="A .battlelab file with a mission plan. To save one of your own, open a mission and choose Save as mission plan in its ⋯ menu."
        >
          {rows.map((p) => (
            <MissionPlanCard key={p.id} plan={p} />
          ))}
        </LibraryGrid>
      )}
      {importer.dialog}
    </>
  );
}

/** The plan detail's ⋯ (#2144): Archive / Unarchive. */
function PlanMenu({
  title,
  archived,
  busy,
  onToggle,
}: {
  title: string;
  archived: boolean;
  busy: boolean;
  onToggle: () => void;
}) {
  return (
    <MoreMenu
      title={title}
      items={[
        {
          key: "archive",
          label: archived ? "Unarchive" : "Archive",
          icon: archived ? <ArchiveRestore size={16} aria-hidden /> : <Archive size={16} aria-hidden />,
          disabled: busy,
          onSelect: onToggle,
        },
      ]}
    />
  );
}

function Detail({ id }: { id: string }) {
  const read = useCallback(() => api.missionPlan(id), [id]);
  const { data, error, loading, reload } = usePlaybookRead(read);
  const exporter = useExportEntry("mission-plan");
  // Import… makes NEW copies only; this plan is unchanged.
  const importer = useImportEntry(() => undefined);
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
      <div className={lib.error} role="alert">
        {error} <Link to={MISSION_PLANS_PATH}>Back to mission plans</Link>
      </div>
    );
  if (loading && !data) return <p role="status">Loading the mission plan…</p>;
  if (!data) return null;
  const doc = data.document;
  const slotLabel = new Map(doc.slots.map((s) => [s.id, s.label]));
  const fromFile = data.origin_kind === "file";
  return (
    <>
      <LibraryDetail
        testId="mission-plan-detail"
        back={{ to: MISSION_PLANS_PATH, label: "All mission plans" }}
        kicker="mission plans"
        kind="Plan"
        version={String(data.version)}
        source={fromFile ? "imported" : "mission"}
        badges={data.archived ? <span className={lib.grade}>Archived</span> : undefined}
        title={data.title}
        bar={
          <ActionBar
            // Plans are inert: there is nothing to start or edit, so Export leads.
            primary={
              data.archived ? undefined : (
                <button type="button" className={buttons.primary} onClick={() => exporter.open(data.id)}>
                  <Download size={16} aria-hidden />
                  Export
                </button>
              )
            }
            onImport={importer.open}
          >
            <PlanMenu
              title={data.title}
              archived={data.archived}
              busy={busy}
              onToggle={() => void archive(data)}
            />
          </ActionBar>
        }
        notices={
          actionError ? (
            <p role="alert" className={lib.error}>
              {actionError}
            </p>
          ) : undefined
        }
        lead={data.summary || undefined}
        facts={
          <>
            <Fact label="Version">v{data.version}</Fact>
            <SourceFact word={fromFile ? <ImportedMark /> : "Saved from a mission"} />
            <Fact label={`Project slots · ${doc.slots.length}`}>
              <ul aria-label="Project slots" className={lib.plain}>
                {doc.slots.map((s) => (
                  <li key={s.id}>
                    <code>{s.id}</code> {s.label}
                  </li>
                ))}
              </ul>
            </Fact>
            <Fact label="Never saved in a plan">
              <ul aria-label="Never saved in a plan" className={`${lib.plain} ${lib.small}`}>
                {NEVER_SAVED.map((t) => (
                  <li key={t}>{t}</li>
                ))}
              </ul>
            </Fact>
          </>
        }
      >
        <p className={styles.inert} data-testid="mission-plan-inert">
          <b>This plan runs nothing.</b> It has no projects, agents or values of its own: a mission
          planned from it later fills each slot with one of your projects, and you approve that
          mission before anything starts.
        </p>
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
      </LibraryDetail>
      {exporter.dialog}
      {importer.dialog}
    </>
  );
}
