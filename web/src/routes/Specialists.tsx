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
 *  on its own. Lowering it stops nothing that runs: new launches wait until the pool is under it. */
function PoolLimit({ live, limit, onSaved }: { live: number; limit: number; onSaved: () => void }) {
  const [text, setText] = useState(String(limit));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const value = Number(text);
  const valid = Number.isInteger(value) && value >= 1 && value <= 32;
  const save = async () => {
    setBusy(true);
    setError("");
    try {
      await api.setPrefs({ pool_max_instances: value });
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn’t save the limit.");
    } finally {
      setBusy(false);
    }
  };
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
      {!valid && (
        <span id="pool-limit-hint" className={styles.refused}>
          A whole number from 1 to 32.
        </span>
      )}
      <button
        type="button"
        className={buttons.ghost}
        disabled={busy || !valid || value === limit}
        onClick={() => void save()}
      >
        Save limit
      </button>
      {error && (
        <span role="alert" className={styles.refused}>
          {error}
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
          key={data.max_instances}
          live={data.live_total ?? 0}
          limit={data.max_instances}
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
