import { Info } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../../lib/api";
import { relTime } from "../../lib/format";
import type { NeedsYouRow } from "../../types/api";
import s from "./AskHome.module.css";
import { KIND_LABEL, approveLabel, sessionRoute } from "./needsYouLabels";
import type { NeedsYouState } from "./useNeedsYou";

/** NEEDS YOU on the Ask page (#1086): sessions no mission holds that need the operator, newest
 *  first, filterable by agent and project. Only sessions that need you are listed.
 *
 *  A row's Approve sends the decision UNEDITED and always names what it does; to read first, edit
 *  a text decision, pick among a menu's options or dismiss, the operator opens ⓘ. Nothing here
 *  mounts a terminal: approving from a row attaches no viewer, so it cannot refuse the way the
 *  session pane's strip did (#1049). Every string that came from an agent is rendered as TEXT. */
export function NeedsYou({
  state,
  facets,
  engine,
  project,
  onEngine,
  onProject,
  onDetails,
  onChanged,
}: {
  state: NeedsYouState;
  /** The last known filter options — kept across filter changes (see `useNeedsYou`). */
  facets: { engines: string[]; projects: { id: string; name: string }[] } | null;
  engine: string;
  project: string;
  onEngine: (v: string) => void;
  onProject: (v: string) => void;
  onDetails: (id: string) => void;
  /** A decision was settled from a row (or Retry was pressed): re-read the list. */
  onChanged: () => void;
}) {
  const data = state.data;
  const filtered = Boolean(engine || project);
  const count = state.status === "ok" ? data?.total ?? 0 : null;

  return (
    <section className={s.sec} aria-labelledby="needs-you-h" data-testid="needs-you">
      <div className={s.head}>
        <span className={s.sq} aria-hidden="true" />
        <h2 id="needs-you-h" className={s.title}>
          Needs you{count !== null ? ` · ${count}` : ""}
        </h2>
        <span className={s.meta}>
          {state.status === "loading"
            ? "reading…"
            : filtered
              ? "in current filters"
              : "sessions without a mission · newest first"}
        </span>
        <div className={s.headEnd}>
          <select
            className={s.select}
            aria-label="Agent"
            value={engine}
            onChange={(e) => onEngine(e.target.value)}
          >
            <option value="">Agent · all</option>
            {(facets?.engines ?? (engine ? [engine] : [])).map((e) => (
              <option key={e} value={e}>
                {e}
              </option>
            ))}
          </select>
          <select
            className={s.select}
            aria-label="Project"
            value={project}
            onChange={(e) => onProject(e.target.value)}
          >
            <option value="">Project · all</option>
            {(facets?.projects ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name || p.id}
              </option>
            ))}
          </select>
          {filtered ? (
            <button
              type="button"
              className={s.btn}
              onClick={() => {
                onEngine("");
                onProject("");
              }}
            >
              Clear filters
            </button>
          ) : null}
        </div>
      </div>
      <Body state={state} filtered={filtered} onDetails={onDetails} onChanged={onChanged} />
    </section>
  );
}

function Body({
  state,
  filtered,
  onDetails,
  onChanged,
}: {
  state: NeedsYouState;
  filtered: boolean;
  onDetails: (id: string) => void;
  onChanged: () => void;
}) {
  if (state.status === "loading" && !state.data) {
    return <p className={s.note}>Reading sessions…</p>;
  }
  if (state.status === "error" && !state.data) {
    // A failed read is NEVER drawn as "nothing needs you": that would be a claim we cannot make.
    return (
      <p className={`${s.note} ${s.err}`} role="alert" data-testid="needs-you-error">
        Couldn’t read sessions — nothing is shown rather than a list that may be wrong.{" "}
        <button type="button" className={s.btn} onClick={onChanged}>
          Retry
        </button>
      </p>
    );
  }
  const rows = state.data?.rows ?? [];
  // A failed REFRESH is handled before "empty" (review 5184): an earlier empty read followed by a
  // failure must never keep claiming "Nothing needs you" — that is exactly the claim we can no
  // longer make.
  const refreshFailed =
    state.status === "error" ? (
      <p className={`${s.note} ${s.err}`} role="alert" data-testid="needs-you-refresh-error">
        Couldn’t refresh — {rows.length ? "showing the last list read." : "the list may be out of date."}{" "}
        <button type="button" className={s.btn} onClick={onChanged}>
          Retry
        </button>
      </p>
    ) : null;
  if (rows.length === 0) {
    return (
      refreshFailed ?? (
        <p className={s.note} data-testid="needs-you-empty">
          {filtered ? "Nothing needs you in these filters." : "Nothing needs you."}
        </p>
      )
    );
  }
  return (
    <div>
      {refreshFailed}
      {rows.map((r) => (
        <Row key={r.id} row={r} onDetails={onDetails} onChanged={onChanged} />
      ))}
      {state.data?.truncated ? (
        <p className={s.note}>
          Showing the newest {rows.length} of {state.data.total}.
        </p>
      ) : null}
    </div>
  );
}

function Row({
  row,
  onDetails,
  onChanged,
}: {
  row: NeedsYouRow;
  onDetails: (id: string) => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = approveLabel(row.action, row.menu);

  const approve = async () => {
    if (!row.action || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.approveAction(row.action.id);
      onChanged();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "The session moved on — open the details to see where it is now."
          : e instanceof Error
            ? e.message
            : "That didn’t go through.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={s.row} data-testid="needs-you-row" data-session={row.id}>
      <span className={s.led} role="img" aria-label="Needs you" />
      <div>
        <Link className={s.rowTitle} to={sessionRoute(row.id)}>
          {row.title || row.id}
        </Link>
        <div className={s.rowMeta}>
          {row.engine}
          {row.project.name ? ` · ${row.project.name}` : ""} · {relTime(row.since)}
        </div>
        <div className={s.why}>
          <span className={s.kind}>{KIND_LABEL[row.kind] ?? row.kind}</span>
          {row.reason || row.action?.rationale || row.summary || "Waiting on you."}
        </div>
        {error ? (
          <div className={s.rowErr} role="alert">
            {error}
          </div>
        ) : null}
      </div>
      <div className={s.ctl}>
        {label ? (
          <button
            type="button"
            className={`${s.btn} ${s.primary}`}
            disabled={busy}
            onClick={() => void approve()}
            data-testid="needs-you-approve"
          >
            {busy ? "Sending…" : label}
          </button>
        ) : null}
        <button
          type="button"
          className={`${s.btn} ${s.icon}`}
          aria-label={`Details for ${row.title || row.id}`}
          title="Details"
          onClick={() => onDetails(row.id)}
          data-testid="needs-you-details"
        >
          <Info size={16} aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
