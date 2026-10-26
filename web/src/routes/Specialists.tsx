/** Library → Specialists (#1693, #1613 P3): the Commander's pool. A specialist is a job — title,
 *  skills, a preselected engine, model and effort, and a capacity — that HQ staffs missions from
 *  and that a session can be started from. Edits make a new version; the grade is derived. */
import { Plus } from "lucide-react";
import { useCallback, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { usePlaybookRead } from "../components/playbooks/usePlaybooks";
import { SpecialistCard } from "../components/specialists/SpecialistCard";
import { SpecialistEditor } from "../components/specialists/SpecialistEditor";
import { api } from "../lib/api";
import { SPECIALIST_NEW_PATH } from "../lib/routes";
import buttons from "../components/ui/actionButton.module.css";
import pb from "../components/playbooks/playbooks.module.css";
import styles from "../components/specialists/specialists.module.css";

export default function Specialists() {
  const { specialistId } = useParams();
  const isNew = useLocation().pathname === SPECIALIST_NEW_PATH;
  return (
    <main className={pb.page} data-testid="specialists-page">
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

function Pool() {
  const read = useCallback(() => api.specialists(), []);
  const { data, error, loading, reload } = usePlaybookRead(read);
  // Archived specialists are hidden from new use, never lost: one toggle shows them instead.
  const [showArchived, setShowArchived] = useState(false);
  const all = data?.specialists ?? [];
  const archivedCount = all.filter((s) => s.archived).length;
  const rows = all.filter((s) => s.archived === showArchived);
  return (
    <>
      <div className={`nj-kicker ${pb.kicker}`}>
        <span className="nj-notch" aria-hidden="true" />
        Library // <b>specialists</b>
        {data ? ` // ${rows.length}` : ""}
      </div>
      <div className={pb.header}>
        <h1>Specialists</h1>
        <Link className={buttons.primary} to={SPECIALIST_NEW_PATH}>
          New specialist
        </Link>
      </div>
      <p className={pb.intro}>
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
      {error && (
        <div className={pb.error} role="alert">
          {error}{" "}
          <button className={buttons.ghost} onClick={() => void reload()}>
            Try again
          </button>
        </div>
      )}
      {loading && !data && <p role="status">Loading specialists…</p>}
      {archivedCount > 0 && (
        <button
          type="button"
          className={buttons.ghost}
          aria-pressed={showArchived}
          data-testid="specialists-show-archived"
          onClick={() => setShowArchived((v) => !v)}
        >
          {showArchived ? "Show active" : `Show archived (${archivedCount})`}
        </button>
      )}
      {data && !rows.length && !showArchived && (
        <div className={pb.empty}>
          <h2>No specialists yet.</h2>
          <p>
            Start from a template (Intern, Implementer, Reviewer, Bookkeeper) and choose
            the agent and model it runs on.
          </p>
        </div>
      )}
      <div className={pb.grid}>
        {rows.map((s, i) => (
          <SpecialistCard key={s.id} specialist={s} ordinal={i + 1} onChanged={() => void reload()} />
        ))}
        {data && !showArchived && (
          <Link to={SPECIALIST_NEW_PATH} className={pb.newCard}>
            <Plus size={22} aria-hidden="true" />
            <span className={pb.newTitle}>New specialist</span>
            <span className={pb.newHint}>Start from a template, or from scratch.</span>
          </Link>
        )}
      </div>
    </>
  );
}
