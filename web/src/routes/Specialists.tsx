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
        <p className={styles.poolLine} data-testid="pool-capacity">
          <b>{data.live_total ?? 0}</b> of <b>{data.max_instances}</b> instances running across
          the pool
        </p>
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
          <SpecialistCard key={s.id} specialist={s} ordinal={i + 1} />
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
