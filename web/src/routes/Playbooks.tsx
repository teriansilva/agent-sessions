/** Library → Playbooks: your playbooks (bundled + local — what projects use) and one playbook's
 *  detail. The public gallery is the sibling tab (#1440). Both wear the one Library gallery
 *  (#2144): a tap on a card opens it, Export is on each card you own, and Import from file closes
 *  the grid. Mutations retain the revision the operator saw. */
import { PLAYBOOK_NEW_PATH } from "../lib/routes";
import { useCallback, useState } from "react";
import { useParams } from "react-router-dom";
import { LandingNote, ViewTabs } from "../components/gallery/ViewTabs";
import { NewPlaybookLink, PlaybookCard } from "../components/playbooks/PlaybookCard";
import { playbookSource } from "../components/playbooks/playbookSource";
import { PlaybookDetailPage } from "../components/playbooks/PlaybookDetail";
import { usePlaybookRead } from "../components/playbooks/usePlaybooks";
import { LibraryGrid, LibraryHead, LibraryToolbar, type Facet } from "../components/library/LibraryGrid";
import type { LibrarySource } from "../components/library/LibraryCard";
import { useImportEntry } from "../components/share/useLibraryShare";
import { useShareImports } from "../components/share/useShareImports";
import { api } from "../lib/api";
import buttons from "../components/ui/actionButton.module.css";
import styles from "../components/library/library.module.css";
// The playbook pages' element rules (fleet, flow preview, instructions) still live here.
import legacy from "../components/playbooks/playbooks.module.css";

export default function Playbooks() {
  const { playbookId } = useParams();
  return (
    <main className={`${legacy.page} ${styles.page}`} data-testid="playbooks-page">
      <LandingNote />
      {playbookId ? (
        <PlaybookDetailPage key={playbookId} id={playbookId} />
      ) : (
        <Library />
      )}
    </main>
  );
}

type Show = "all" | LibrarySource;
const FACETS: { value: Show; label: string }[] = [
  { value: "all", label: "All" },
  { value: "local", label: "Local" },
  { value: "imported", label: "Imported" },
  { value: "gallery", label: "Gallery" },
  { value: "bundled", label: "Bundled" },
  { value: "catalog", label: "Catalog" },
];

function Library() {
  const read = useCallback(() => api.playbooks(), []);
  const { data, error, loading, reload } = usePlaybookRead(read);
  const [search, setSearch] = useState("");
  const [show, setShow] = useState<Show>("all");
  const [round, setRound] = useState(0);
  const imported = useShareImports(round);
  const importer = useImportEntry(() => {
    setRound((n) => n + 1);
    void reload();
  });
  const rows = (data?.playbooks ?? []).map((p) => ({
    p,
    source: playbookSource(p, imported.has(`playbook:${p.id}`)),
  }));
  const needle = search.trim().toLowerCase();
  const matching = rows.filter(({ p }) =>
    [
      p.id,
      p.name,
      p.summary,
      p.domain,
      ...(p.flows ?? []).flatMap((f) =>
        f.steps.flatMap((s) => [s.title, s.actor.engine, s.actor.label, s.actor.model]),
      ),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(needle),
  );
  const shown = matching.filter((r) => show === "all" || r.source === show);
  const facets: Facet<Show>[] = FACETS.map((f) => ({
    ...f,
    n: f.value === "all" ? matching.length : matching.filter((r) => r.source === f.value).length,
  })).filter((f) => f.value === "all" || f.value === show || f.n > 0);
  return (
    <>
      <LibraryHead
        section="playbooks"
        title="Playbooks"
        count={data ? rows.length : undefined}
        primary={<NewPlaybookLink />}
      />
      <ViewTabs kind="playbook" view="local" />
      <p className={styles.intro}>
        How a repository works: its flow of agents, the checklist that ends each step, and the
        files, templates and variables a project is given. New projects choose from these.
      </p>
      <LibraryToolbar
        label="Search playbooks"
        placeholder="Name, summary, step or agent…"
        search={search}
        onSearch={setSearch}
        facets={data ? facets : undefined}
        facet={show}
        onFacet={setShow}
        count={data ? `${shown.length} ${shown.length === 1 ? "playbook" : "playbooks"}` : undefined}
      />
      {error && (
        <div className={styles.error} role="alert">
          {error}
          {data ? " Showing the last successful read." : ""}{" "}
          <button className={buttons.ghost} disabled={loading} onClick={() => void reload()}>
            Try again
          </button>
        </div>
      )}
      {loading && !data && <p role="status">Loading playbooks…</p>}
      {data && !rows.length && (
        <div className={styles.empty}>
          <h2>No playbooks on this host.</h2>
          <p>
            Playbooks from installed bundles, the ones you create, import from a file or add from
            the public gallery appear here.
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
              setShow("all");
            }}
          >
            Clear filters
          </button>
        </div>
      )}
      {/* The tiles stay reachable when the list can't be read: importing doesn't need it. */}
      {(data || error) && (
        <LibraryGrid
          testId="playbook-grid"
          newTile={{
            to: PLAYBOOK_NEW_PATH,
            title: "New playbook",
            hint: "Start from a flow, or copy one from the public gallery.",
          }}
          onImport={importer.open}
        >
          {shown.map(({ p, source }, i) => (
            <PlaybookCard key={`${p.source}:${p.id}:${i}`} card={p} source={source} />
          ))}
        </LibraryGrid>
      )}
      {importer.dialog}
      {!!data?.recovery_total && (
        <p className={styles.intro}>
          {data.recovery_total} previous or interrupted copies are retained for recovery.
        </p>
      )}
    </>
  );
}
