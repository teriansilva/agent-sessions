/** Library's bundle gallery and detail. Mutations retain the revision the operator saw. */
import { useCallback, useState } from "react";
import { useParams } from "react-router-dom";
import { HudFrame } from "../components/hud/HudFrame";
import { PlaybookCard } from "../components/playbooks/PlaybookCard";
import { PlaybookDetailPage } from "../components/playbooks/PlaybookDetail";
import { usePlaybookRead } from "../components/playbooks/usePlaybooks";
import { api } from "../lib/api";
import buttons from "../components/ui/actionButton.module.css";
import styles from "../components/playbooks/playbooks.module.css";

export default function Playbooks() {
  const { playbookId } = useParams();
  return (
    <main className={styles.page} data-testid="playbooks-page">
      <HudFrame hero />
      {playbookId ? (
        <PlaybookDetailPage key={playbookId} id={playbookId} />
      ) : (
        <Gallery />
      )}
    </main>
  );
}

function Gallery() {
  const read = useCallback(() => api.playbooks(), []);
  const { data, error, loading, reload } = usePlaybookRead(read);
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("");
  const [domain, setDomain] = useState("");
  const rows = data?.playbooks ?? [];
  const needle = search.trim().toLowerCase();
  const shown = rows.filter(
    (p) =>
      (!source || p.source === source) &&
      (!domain || p.domain === domain) &&
      [
        p.id,
        p.name,
        p.summary,
        ...(p.flows ?? []).flatMap((f) =>
          f.steps.flatMap((s) => [
            s.title,
            s.actor.engine,
            s.actor.label,
            s.actor.model,
          ]),
        ),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(needle),
  );
  return (
    <>
      <div className={styles.kicker}>
        Library // playbooks{data ? ` // ${rows.length}` : ""}
      </div>
      <h1>Playbooks</h1>
      <p className={styles.intro}>
        How a repository works: its flow of agents, the checklist that ends each
        step, and the files, templates and variables a project is given.
      </p>
      <div className={styles.filters}>
        <label>
          Search playbooks
          <input
            type="search"
            value={search}
            placeholder="Name, summary, step or agent…"
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <label>
          Source
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="">All sources</option>
            <option value="local">Local</option>
            <option value="bundled">Bundled</option>
            <option value="catalog">Catalog</option>
          </select>
        </label>
        <label>
          Domain
          <select value={domain} onChange={(e) => setDomain(e.target.value)}>
            <option value="">All domains</option>
            {[
              ...new Set(
                rows.map((p) => p.domain).filter((d): d is string => !!d),
              ),
            ]
              .sort()
              .map((d) => (
                <option key={d}>{d}</option>
              ))}
          </select>
        </label>
        <button
          className={buttons.ghost}
          disabled={loading}
          onClick={() => void reload()}
        >
          Refresh
        </button>
      </div>
      {error && (
        <div className={styles.error} role="alert">
          {error}
          {data ? " Showing the last successful read." : ""}{" "}
          <button className={buttons.ghost} onClick={() => void reload()}>
            Try again
          </button>
        </div>
      )}
      {loading && !data && <p role="status">Loading playbooks…</p>}
      {data && !rows.length && (
        <div className={styles.empty}>
          <h2>No playbooks on this host.</h2>
          <p>
            Playbooks from installed bundles and your local library will appear
            here.
          </p>
        </div>
      )}
      {data && !!rows.length && !shown.length && (
        <div className={styles.empty}>
          <h2>No matching playbooks</h2>
          <button
            className={buttons.ghost}
            onClick={() => {
              setSearch("");
              setSource("");
              setDomain("");
            }}
          >
            Clear filters
          </button>
        </div>
      )}
      <div className={styles.grid}>
        {shown.map((card, i) => (
          <PlaybookCard key={`${card.source}:${card.id}:${i}`} card={card} />
        ))}
      </div>
      {!!data?.recovery_total && (
        <p>
          {data.recovery_total} previous or interrupted copies are retained for
          recovery.
        </p>
      )}
    </>
  );
}
