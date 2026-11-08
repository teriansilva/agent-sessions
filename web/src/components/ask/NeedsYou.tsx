import { Info } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../../lib/api";
import { relTime } from "../../lib/format";
import type { AgentReport, NeedsYouRow } from "../../types/api";
import { Loader } from "../hud/Loader";
import { Led } from "../hud/Meters";
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
  /** Open a row's details. ``shownReportId``: the agent's report the row DISPLAYS, so a
   *  dismissal from the details covers that report and never one it didn't show (#1882). */
  onDetails: (id: string, shownReportId?: string | null) => void;
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
  onDetails: (id: string, shownReportId?: string | null) => void;
  onChanged: () => void;
}) {
  if (state.status === "loading" && !state.data) {
    return <Loader kind="acquire" label="Acquiring sessions" className={s.loading} />;
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
        // Keyed by the REPORT too (#1882): a row whose report was replaced is a new row — a tap
        // still in flight for the old report keeps no "Sending…" here and its late result or
        // error is dropped with the old row, never shown against the new report.
        <Row
          key={r.report ? `${r.id}:${r.report.id}` : r.id}
          row={r}
          onDetails={onDetails}
          onChanged={onChanged}
        />
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
  onDetails: (id: string, shownReportId?: string | null) => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<RowError | null>(null);
  const label = approveLabel(row.action, row.menu);

  const approve = async () => {
    if (!row.action || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.approveAction(row.action.id);
      onChanged();
    } catch (e) {
      setError({
        text:
          e instanceof ApiError && e.status === 409
            ? "The session moved on — open the details to see where it is now."
            : e instanceof Error
              ? e.message
              : "That didn’t go through.",
      });
    } finally {
      setBusy(false);
    }
  };

  // An agent's own report (#1882) is drawn as its words, with its options, when the row carries
  // no pending decision of its own (a pending decision is what the row's controls settle).
  const report = !row.action && row.report ? row.report : null;

  const dismissReport = async () => {
    if (!report || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.dismissReport(row.id, report.id);
      onChanged();
    } catch (e) {
      setError({ text: e instanceof Error ? e.message : "That didn’t go through." });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className={s.row}
      data-testid="needs-you-row"
      data-session={row.id}
      data-kind={row.kind}
    >
      <Led tone="att" label="Needs you" className={s.led} />
      <div>
        <Link className={s.rowTitle} to={sessionRoute(row.id)}>
          {row.title || row.id}
        </Link>
        <div className={s.rowMeta}>
          {row.engine}
          {row.project.name ? ` · ${row.project.name}` : ""} · {relTime(row.since)}
        </div>
        {report ? (
          <>
            <div className={s.why}>
              <span className={s.kind}>{KIND_LABEL[row.kind] ?? row.kind}</span>
            </div>
            <ReportBody
              row={row}
              report={report}
              busy={busy}
              setBusy={setBusy}
              setError={setError}
              onChanged={onChanged}
            />
          </>
        ) : (
          <div className={s.why}>
            <span className={s.kind}>{KIND_LABEL[row.kind] ?? row.kind}</span>
            {row.reason || row.action?.rationale || row.summary || "Waiting on you."}
          </div>
        )}
        {error ? (
          <div className={s.rowErr} role="alert" data-testid="needs-you-row-error">
            {error.text}
            {error.open ? (
              <>
                {" "}
                <Link className={s.reportOpen} to={sessionRoute(row.id)}>
                  Open session
                </Link>
              </>
            ) : null}
          </div>
        ) : null}
      </div>
      <div className={s.ctl}>
        {report ? (
          <button
            type="button"
            className={`${s.quiet} ${s.reportDismiss}`}
            disabled={busy}
            onClick={() => void dismissReport()}
            data-testid="needs-you-report-dismiss"
          >
            {/* ENDS the agent's report (#1882) — not the screen "hide" Details offers. */}
            Dismiss report
          </button>
        ) : null}
        {label ? (
          <button
            type="button"
            className={s.primary}
            disabled={busy}
            onClick={() => void approve()}
            data-testid="needs-you-approve"
          >
            {busy ? "Sending…" : label}
          </button>
        ) : null}
        <button
          type="button"
          className={s.icon}
          aria-label={`Details for ${row.title || row.id}`}
          title="Details"
          onClick={() => onDetails(row.id, report?.id ?? null)}
          data-testid="needs-you-details"
        >
          <Info size={16} aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}

/** The agent's own report (#1882): its words, framed as unchecked agent text (rendered as TEXT,
 *  never markup), and for a decision its numbered options. A tap on an option IS the approval:
 *  the server types exactly `Decision: <option>` into the session, or refuses when the report or
 *  its launch moved on. When delivery is impossible the options are disabled, with the server's
 *  reason and a way to the session. */
function ReportBody({
  row,
  report,
  busy,
  setBusy,
  setError,
  onChanged,
}: {
  row: NeedsYouRow;
  report: AgentReport;
  busy: boolean;
  setBusy: (v: boolean) => void;
  setError: (v: RowError | null) => void;
  onChanged: () => void;
}) {
  const [sending, setSending] = useState<number | null>(null);
  // A tap that may have reached the agent LOCKS this report's options (#1882, Hermes on #1884):
  // only an explicit "nothing was sent" (or a refusal the server gave before anything was minted)
  // gives them back. The row is keyed by report id, so a newer report starts unlocked.
  const [locked, setLocked] = useState(false);
  const options = report.state === "needs_decision" ? report.options ?? [] : [];
  const answer = row.report_answer ?? { available: false, reason: "" };
  const reasonId = `report-reason-${row.id}`;

  const pick = async (n: number) => {
    if (busy || locked || !answer.available) return;
    setBusy(true);
    setSending(n);
    setError(null);
    let reopen = false;
    try {
      const done = await api.answerReport(row.id, report.id, n);
      // Success only for `delivered`: the server answers 200 for an attempted write too.
      if (done.outcome === "uncertain") {
        setError({ text: UNCERTAIN, open: true });
      } else if (done.outcome === "stale") {
        reopen = true;
        setError({
          text: "Nothing was sent — the session couldn’t take it. Open the session to see where it is now.",
          open: true,
        });
      }
      onChanged();
    } catch (e) {
      const refused = reportRefusal(e);
      reopen = refused.reopen === true;
      setError(refused);
    } finally {
      setLocked(!reopen);
      setSending(null);
      setBusy(false);
    }
  };

  return (
    <div className={s.report} data-testid="needs-you-report">
      <div className={s.reportQuote}>
        <div className={s.reportLab}>
          The agent says <span className={s.reportLabNote}>— its own words, unchecked</span>
        </div>
        <p className={s.reportSummary} data-testid="needs-you-report-summary">
          {report.summary}
        </p>
        {report.question ? (
          <p className={s.reportQuestion} data-testid="needs-you-report-question">
            {report.question}
          </p>
        ) : null}
      </div>
      {options.length ? (
        <>
          <div className={s.reportOpts} role="group" aria-label="Options">
            {options.map((label, i) => (
              <button
                key={`${i}-${label}`}
                type="button"
                className={s.reportOpt}
                disabled={busy || locked || !answer.available}
                aria-describedby={answer.available ? undefined : reasonId}
                onClick={() => void pick(i + 1)}
                data-testid="needs-you-report-option"
              >
                <span className={s.reportOptNum} aria-hidden="true">
                  {i + 1}
                </span>
                <span className={s.reportOptLabel}>
                  {sending === i + 1 ? "Sending…" : label}
                </span>
              </button>
            ))}
          </div>
          {answer.available ? (
            <p className={s.reportHint}>
              A tap sends it: the session receives <code>Decision: &lt;option&gt;</code> as if
              you typed it.
            </p>
          ) : (
            <p className={s.reportHint} id={reasonId} data-testid="needs-you-report-reason">
              {answer.reason}{" "}
              <Link className={s.reportOpen} to={sessionRoute(row.id)}>
                Open session
              </Link>
            </p>
          )}
        </>
      ) : null}
    </div>
  );
}

/** An answer whose write was attempted and not confirmed, or one already sent: never "sent",
 *  never "nothing was sent". */
const UNCERTAIN =
  "This answer may already have been sent — the session didn’t confirm it. Check the session before answering again.";

/** A row's error line; ``open`` adds the way to the session; ``reopen``: the tap provably sent
 *  nothing, so the report's options may be offered again. */
type RowError = { text: string; open?: boolean; reopen?: boolean };

/** What a refused option tap tells the Commander (#1882). The server says WHICH refusal it is:
 *  only `stale` proves nothing was typed, so only it may say "Nothing was sent" and give the
 *  options back; `spent` / `uncertain` mean an answer may already have been sent, so the session
 *  must be checked first; `unknown` (a store can't be read) claims neither. A failure where the
 *  request may have reached the server — the network, a lost response, a 5xx or a 409 without a
 *  code — is treated as possibly sent. A refusal the server gives before anything is minted (a
 *  4xx other than 409) sends nothing. */
function reportRefusal(e: unknown): RowError {
  if (!(e instanceof ApiError)) {
    // No answer from the server: the request may have reached it and been typed.
    return {
      text: "This answer may already have been sent — the connection dropped before BattleLab replied. Check the session before answering again.",
      open: true,
    };
  }
  const code = (e.record as { code?: unknown } | undefined)?.code;
  if (code === "unknown") {
    // Whether an earlier answer was sent can't be read: refused, and claims neither.
    return { text: `${e.message}. Open the session to check.`, open: true };
  }
  if (e.status === 409 && code === "stale") {
    return {
      text: `Nothing was sent — ${e.message || "the agent’s report moved on"}. Open the session to see where it is now.`,
      open: true,
      reopen: true,
    };
  }
  if (code === "spent" || code === "uncertain") {
    return {
      text: "An answer to this may already have been sent. Check the session before answering again.",
      open: true,
    };
  }
  if (e.status >= 400 && e.status < 500 && e.status !== 409) {
    return { text: e.message, reopen: true };
  }
  return { text: UNCERTAIN, open: true };
}
