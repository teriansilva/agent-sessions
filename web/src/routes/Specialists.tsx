/** Library → Specialists (#1693, #1613 P3): the Commander's pool. A specialist is a job — title,
 *  skills, a preselected engine, model and effort, and a capacity — that HQ staffs missions from
 *  and that a session can be started from. Edits make a new version; the grade is derived. */
import { Plus } from "lucide-react";
import { useCallback, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { usePlaybookRead } from "../components/playbooks/usePlaybooks";
import { SpecialistCard } from "../components/specialists/SpecialistCard";
import { SpecialistEditor } from "../components/specialists/SpecialistEditor";
import { useImportEntry } from "../components/share/useLibraryShare";
import { LibraryGrid, LibraryHead, LibraryToolbar, type Facet } from "../components/library/LibraryGrid";
import { GRADES, GRADE_LABEL } from "../components/specialists/grades";
import type { Grade } from "../types/specialists";
import lib from "../components/library/library.module.css";
import { useShareImports } from "../components/share/useShareImports";
import { api } from "../lib/api";
import { reserveHelp, reserveProblem } from "../lib/automationQueue";
import { SPECIALIST_NEW_PATH } from "../lib/routes";
import buttons from "../components/ui/actionButton.module.css";
import pb from "../components/playbooks/playbooks.module.css";
import styles from "../components/specialists/specialists.module.css";

export default function Specialists() {
  const { specialistId } = useParams();
  const isNew = useLocation().pathname === SPECIALIST_NEW_PATH;
  return (
    <main className={`${pb.page} ${lib.page}`} data-testid="specialists-page">
      {isNew ? (
        <SpecialistEditor key="new" />
      ) : specialistId ? (
        <SpecialistEditor key={specialistId} id={specialistId} />
      ) : (
        <Pool />
      )}
    </main>
  );
}

/** The pool's line (#1693 S4): instances running against the limit, and the limit itself, saved
 *  on its own. Lowering it stops nothing that runs: new launches wait until the pool is under it.
 *  #2422: part of the pool is reserved for automations, saved with the limit in ONE request; the
 *  help line says what that leaves for every other start. The server refuses a reserve above the
 *  limit (422) and stays the authority; the form only keeps an obviously refused save unsent. */
function PoolLimit({
  live,
  limit,
  reserved,
  onSaved,
}: {
  live: number;
  limit: number;
  reserved: number;
  onSaved: () => void;
}) {
  const [text, setText] = useState(String(limit));
  const [reserveText, setReserveText] = useState(String(reserved));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const value = Number(text);
  const reserve = Number(reserveText);
  const valid = Number.isInteger(value) && value >= 1 && value <= 32;
  const reserveValid = Number.isInteger(reserve) && reserve >= 1 && reserve <= 32;
  const changed = value !== limit || reserve !== reserved;
  // A stored reserve above the limit is only in force up to it (`reserved_effective`): it is said
  // in the help line, and refused only when the operator tries to save it.
  const tooMany =
    changed && valid && reserveValid ? reserveProblem(value, reserve, "pool limit") : "";
  const save = async () => {
    setBusy(true);
    setError("");
    try {
      await api.setPrefs({ pool_max_instances: value, pool_reserved_automations: reserve });
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn’t save the limits.");
    } finally {
      setBusy(false);
    }
  };
  const hint = !valid
    ? "A whole number from 1 to 32."
    : !reserveValid
      ? "Reserve a whole number from 1 to 32."
      : tooMany;
  return (
    <div className={styles.poolLine} data-testid="pool-capacity">
      <span className={live > limit ? styles.overCapacity : undefined}>
        <b>{live}</b> of <b>{limit}</b> instances running across the pool
        {live > limit && " · over the limit: no new instances until some end"}
      </span>
      <label className={styles.poolLimit}>
        Pool limit
        <input
          type="number"
          min={1}
          max={32}
          value={text}
          aria-invalid={!valid}
          aria-describedby={valid ? undefined : "pool-limit-hint"}
          onChange={(e) => setText(e.target.value)}
        />
      </label>
      <label className={styles.poolLimit}>
        Reserved for automations
        <input
          type="number"
          min={1}
          max={32}
          value={reserveText}
          aria-invalid={!reserveValid || !!tooMany}
          aria-describedby={valid && !hint ? "pool-reserve-help" : "pool-limit-hint"}
          onChange={(e) => setReserveText(e.target.value)}
        />
      </label>
      <button
        type="button"
        className={buttons.ghost}
        disabled={busy || !!hint || !changed}
        onClick={() => void save()}
      >
        Save limits
      </button>
      {hint && (
        <span id="pool-limit-hint" className={styles.refused}>
          {hint}
        </span>
      )}
      {error && (
        <span role="alert" className={styles.refused}>
          {error}
        </span>
      )}
      {!hint && (
        <span id="pool-reserve-help" className={styles.poolHelp} data-testid="pool-reserve-help">
          {reserveHelp(value, reserve, "pool")}
        </span>
      )}
    </div>
  );
}

type Show = "all" | Grade | "ungraded" | "archived";

function Pool() {
  const read = useCallback(() => api.specialists(), []);
  const { data, error, loading, reload } = usePlaybookRead(read);
  const [search, setSearch] = useState("");
  // Archived specialists are hidden from new use, never lost: the Archived chip shows them.
  const [show, setShow] = useState<Show>("all");
  const [round, setRound] = useState(0);
  const imported = useShareImports(round);
  const importer = useImportEntry(() => {
    setRound((n) => n + 1);
    void reload();
  });
  const all = data?.specialists ?? [];
  const needle = search.trim().toLowerCase();
  const matching = all.filter((s) =>
    [
      s.id,
      s.current.title,
      s.current.summary,
      s.current.engine,
      s.current.model,
      ...s.current.skills.map((k) => k.id),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(needle),
  );
  const active = matching.filter((s) => !s.archived);
  const pick = (v: Show) =>
    v === "archived"
      ? matching.filter((s) => s.archived)
      : v === "all"
        ? active
        : v === "ungraded"
          ? active.filter((s) => !s.grade)
          : active.filter((s) => s.grade === v);
  const rows = pick(show);
  const facets: Facet<Show>[] = (
    [
      { value: "all", label: "All" },
      ...[...GRADES].reverse().map((g) => ({ value: g, label: GRADE_LABEL[g] })),
      { value: "ungraded", label: "Ungraded" },
      { value: "archived", label: "Archived" },
    ] as { value: Show; label: string }[]
  )
    .map((f) => ({ ...f, n: pick(f.value).length }))
    .filter((f) => f.value === "all" || f.value === show || f.n > 0);
  return (
    <>
      <LibraryHead
        section="specialists"
        title="Specialists"
        count={data ? active.length : undefined}
        primary={
          <Link className={buttons.primary} to={SPECIALIST_NEW_PATH}>
            <Plus size={16} aria-hidden />
            New specialist
          </Link>
        }
      />
      <p className={lib.intro}>
        The jobs HQ staffs your missions from. Each one has the skills it brings, the
        agent and model it runs on, and how many can work at once. Its grade comes
        from the model: map your models in Settings → Agents.
      </p>
      {data?.max_instances !== undefined && (
        <PoolLimit
          key={`${data.max_instances}:${data.reserved_automations ?? 1}`}
          live={data.live_total ?? 0}
          limit={data.max_instances}
          reserved={data.reserved_automations ?? 1}
          onSaved={() => void reload()}
        />
      )}
      <LibraryToolbar
        label="Search specialists"
        placeholder="Title, skill or model…"
        search={search}
        onSearch={setSearch}
        facets={data ? facets : undefined}
        facet={show}
        onFacet={setShow}
        count={data ? `${rows.length} ${rows.length === 1 ? "specialist" : "specialists"}` : undefined}
      />
      {error && (
        <div className={lib.error} role="alert">
          {error}{" "}
          <button className={buttons.ghost} onClick={() => void reload()}>
            Try again
          </button>
        </div>
      )}
      {loading && !data && <p role="status">Loading specialists…</p>}
      {data && !all.some((s) => !s.archived) && show === "all" && (
        <div className={lib.empty}>
          <h2>No specialists yet.</h2>
          <p>
            Start from a template (Intern, Implementer, Reviewer, Bookkeeper, Researcher) and
            choose the agent and model it runs on.
          </p>
        </div>
      )}
      {(data || error) && (
        <LibraryGrid
          testId="specialist-grid"
          newTile={
            show === "archived"
              ? undefined
              : {
                  to: SPECIALIST_NEW_PATH,
                  title: "New specialist",
                  hint: "Start from a template (Intern, Implementer, Reviewer, Bookkeeper, Researcher), or from scratch.",
                }
          }
          onImport={show === "archived" ? undefined : importer.open}
          importHint="A .battlelab file someone shared with you."
        >
          {rows.map((s) => (
            <SpecialistCard
              key={s.id}
              specialist={s}
              imported={imported.has(`specialist:${s.id}`)}
              onChanged={() => void reload()}
            />
          ))}
        </LibraryGrid>
      )}
      {importer.dialog}
    </>
  );
}
