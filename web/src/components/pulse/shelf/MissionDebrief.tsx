import { useId, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, Eye, GitBranch, RefreshCw } from "lucide-react";
import { settingsPath } from "../../../routes/settingsTabs";
import type {
  Debrief,
  DebriefFacts,
  DebriefItem,
  DebriefObjective,
  ShelfKind,
} from "../../../types/shelf";
import action from "../../ui/actionButton.module.css";
import { sourceLabel } from "../judgment";
import { KIND_LABEL, engineOf } from "./shelfLogic";
import { StateWord } from "./ShelfTile";
import type { MissionDebrief as DebriefData } from "./useMissionDebrief";
import styles from "./debrief.module.css";

const KIND_ORDER: ShelfKind[] = ["image", "video", "audio", "document", "code", "link", "text"];

const STATE_HEAD: Record<string, string> = {
  review: "In review",
  done: "Done",
  failed: "Failed",
  abandoned: "Abandoned",
};

/** "Read" is remembered per mission and records revision: new records open it again. */
const READ_KEY = (missionId: string) => `battlelab.debrief.read.${missionId}`;

function wasRead(missionId: string, revision: string): boolean {
  try {
    return window.localStorage.getItem(READ_KEY(missionId)) === revision;
  } catch {
    return false;
  }
}

function markRead(missionId: string, revision: string | null) {
  try {
    if (revision) window.localStorage.setItem(READ_KEY(missionId), revision);
    else window.localStorage.removeItem(READ_KEY(missionId));
  } catch {
    /* private mode: it simply stays open */
  }
}

/** When the summary was written: the day too, since a closed mission is read days later. */
function writtenAt(ts: number | null | undefined): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function objectiveWord(o: Pick<DebriefObjective, "state" | "judged">) {
  if (o.state === "met") return <StateWord word="Met" led="up" tone="ok" />;
  if (o.state === "waived") return <StateWord word="Waived" led="idle" tone="idle" />;
  if (o.state === "failed") return <StateWord word="Failed" led="attention" tone="warn" />;
  return <StateWord word="Not yet" led="attention" tone="warn" />;
}

function judgedLine(o: Pick<DebriefObjective, "judged">) {
  const c = o.judged?.confidence;
  return typeof c === "number" ? <span className={styles.judged}>judged {c.toFixed(2)}</span> : null;
}

/**
 * The mission debrief (#1740 P2): first on SHELF once a mission is in review or closed (design
 * decisions 1, 8, 10, 11). Everything but the "what was done" paragraph is the records themselves,
 * read now; the paragraph is the stored summary, marked out of date the moment the records move
 * past the revision it was written from. Without an AI endpoint the same facts are the debrief.
 */
export function MissionDebrief({
  missionId,
  data,
  configured,
  sessionLabel,
  onReview,
}: {
  missionId: string;
  data: DebriefData;
  configured: boolean;
  sessionLabel: (key: string | null) => string;
  /** Show the unreviewed items (the shelf's "Unreviewed only"). */
  onReview: () => void;
}) {
  const { debrief, error, regenError, regenerating, regenerate } = data;
  const ids = useId();
  const revision = debrief?.facts.revision ?? "";
  /** The operator's own toggle, for the records it was made on: new records open it again even
   *  while the page stays mounted. */
  const [collapsed, setCollapsed] = useState<{ revision: string; value: boolean } | null>(null);
  const isCollapsed =
    collapsed?.revision === revision
      ? collapsed.value
      : revision
        ? wasRead(missionId, revision)
        : false;

  if (!debrief) {
    return (
      <section className={styles.debrief} data-testid="mission-debrief" data-state="loading">
        <p className={styles.lbl}>{error ?? "Reading the debrief…"}</p>
      </section>
    );
  }
  const f = debrief.facts;
  const c = f.shelf.counts;
  const metCount = f.objectives.filter((o) => o.met).length;
  const openCount =
    f.open.objectives_total + f.open.needs_work_total + f.open.found_not_delivered_total;
  const ai = configured && debrief.configured;
  const summary = debrief.summary;
  const stale = !!summary && !debrief.fresh;

  const toggle = () => {
    const next = !isCollapsed;
    setCollapsed({ revision, value: next });
    markRead(missionId, next ? revision : null);
  };
  const review = () => {
    markRead(missionId, revision);
    setCollapsed({ revision, value: true });
    onReview();
  };

  const head = (
    <div className={styles.head}>
      <div className="nj-callsign">
        <span className="nj-notch" aria-hidden="true" />
        <span className="nj-title" id={`${ids}-title`}>
          Debrief
        </span>
        <span>//</span>
        <span className="nj-value">{STATE_HEAD[f.state] ?? f.state}</span>
      </div>
      <button
        type="button"
        className={styles.collapse}
        aria-expanded={!isCollapsed}
        aria-controls={isCollapsed ? undefined : `${ids}-body`}
        onClick={toggle}
        data-debrief-toggle=""
      >
        <ChevronDown aria-hidden="true" className={isCollapsed ? "" : styles.flip} />
        {isCollapsed ? "Expand" : "Collapse"}
      </button>
    </div>
  );

  if (isCollapsed) {
    return (
      <section
        className={`${styles.debrief} ${styles.isCollapsed}`}
        aria-labelledby={`${ids}-title`}
        data-testid="mission-debrief"
        data-collapsed=""
        data-fresh={debrief.fresh ? "" : undefined}
      >
        {head}
        <p className={styles.oneLine} data-debrief-line="">
          {metCount} of {f.objectives.length} objectives met · {c.total} on the shelf ·{" "}
          {c.unreviewed} to review · {openCount} still open
          {stale ? " · summary out of date" : ""}
        </p>
      </section>
    );
  }

  return (
    <section
      className={styles.debrief}
      aria-labelledby={`${ids}-title`}
      data-testid="mission-debrief"
      data-fresh={debrief.fresh ? "" : undefined}
      data-mode={ai ? "summary" : "records"}
    >
      {head}
      <div id={`${ids}-body`} className={styles.body}>
        <div className={styles.meta}>
          {ai ? (
            <>
              <span className="nj-stamp is-hud" data-debrief-tag="">
                AI summary · from the records
              </span>
              {summary ? (
                <span className={styles.lbl} data-debrief-written="">
                  Written {writtenAt(summary.written_at)} · from the records as they were then
                </span>
              ) : null}
              <button
                type="button"
                className={`${action.ghost} ${action.compact} ${styles.regen}`}
                onClick={() => void regenerate()}
                disabled={regenerating || debrief.generating}
                data-debrief-regenerate=""
              >
                <RefreshCw size={14} aria-hidden="true" />
                {regenerating || debrief.generating ? "Writing…" : "Regenerate"}
              </button>
            </>
          ) : (
            <p className={styles.noAi} data-debrief-no-ai="">
              No AI endpoint is configured, so the debrief is the records themselves, not a
              summary. <Link to={settingsPath("ai-endpoint")}>Set up an endpoint</Link>
            </p>
          )}
        </div>
        {stale ? (
          <p className={styles.stale} role="status" data-debrief-stale="">
            <span className="hud-led attention" aria-hidden="true" />
            Out of date: the records changed after this summary was written. The facts below are
            current.
          </p>
        ) : null}
        {regenError ? (
          <p className={styles.stale} role="alert" data-debrief-error="">
            {regenError}
          </p>
        ) : null}

        <div className={styles.grid}>
          <div className={styles.main}>
            <section>
              <h4 className={styles.lbl}>What was asked</h4>
              <blockquote className={styles.asked} data-debrief-asked="">
                {f.asked}
              </blockquote>
            </section>
            {ai ? (
              <section>
                <h4 className={styles.lbl}>What was done</h4>
                {summary ? (
                  <p
                    className={`${styles.done} ${stale ? styles.isStale : ""}`}
                    data-debrief-summary=""
                  >
                    {summary.text}
                  </p>
                ) : debrief.generating ? (
                  <p className={styles.muted}>Writing the summary from the records…</p>
                ) : (
                  <p className={styles.muted} data-debrief-summary-error="">
                    {debrief.error
                      ? `No summary: ${debrief.error}.`
                      : "No summary yet. Regenerate writes one."}
                  </p>
                )}
              </section>
            ) : (
              <Sessions facts={f} sessionLabel={sessionLabel} />
            )}
          </div>
          <OnTheShelf facts={f} sessionLabel={sessionLabel} groups={!ai} />
        </div>

        <section>
          <h4 className={styles.lbl}>Objectives · judged with the judge's own words</h4>
          {f.objectives.length === 0 ? (
            <p className={styles.muted}>This mission had no objectives.</p>
          ) : (
            <ul className={styles.objectives} data-debrief-objectives="">
              {f.objectives.map((o) => (
                <li key={o.key} className={styles.objective} data-debrief-objective={o.key}>
                  <span className={styles.objState}>{objectiveWord(o)}</span>
                  <div className={styles.objBody}>
                    <p className={styles.objTitle}>
                      {o.title} {judgedLine(o)}
                    </p>
                    {(o.judged?.evidence ?? []).map((e, i) => (
                      <figure key={i} className={styles.quote}>
                        <blockquote>“{e.quote}”</blockquote>
                        <figcaption className={styles.lbl}>{sourceLabel(e.source)}</figcaption>
                      </figure>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section>
          <h4 className={styles.lbl}>Still open · three separate lists</h4>
          <div className={styles.open}>
            <OpenList
              id="objectives"
              title="Objectives not judged met"
              total={f.open.objectives_total}
              shown={f.open.objectives.length}
            >
              {f.open.objectives.map((o) => (
                <li key={o.key}>
                  {objectiveWord(o)}
                  <span className={styles.rowText}>
                    {o.title} {judgedLine(o)}
                  </span>
                </li>
              ))}
            </OpenList>
            <OpenList
              id="needs_work"
              title="Items marked needs work"
              total={f.open.needs_work_total}
              shown={f.open.needs_work.length}
            >
              {f.open.needs_work.map((i) => (
                <li key={i.id}>
                  <StateWord word="Needs work" led="attention" tone="warn" />
                  <span className={styles.rowText}>
                    <b>{i.title}</b>
                    {i.note ? <span className={styles.note}> “{i.note}”</span> : null}
                  </span>
                </li>
              ))}
            </OpenList>
            <OpenList
              id="found"
              title="Found in git, not delivered by an agent"
              total={f.open.found_not_delivered_total}
              shown={f.open.found_not_delivered.length}
              hint="Provenance, not necessarily unfinished work."
            >
              {f.open.found_not_delivered.map((i) => (
                <li key={i.id} className={styles.neutral}>
                  <GitBranch size={14} aria-hidden="true" />
                  <span className={styles.rowText}>
                    {i.title} <FoundMeta item={i} />
                  </span>
                </li>
              ))}
            </OpenList>
          </div>
        </section>

        <div className={styles.foot}>
          <p className={styles.muted}>
            Verdicts are recorded on the mission. You still close it: <b>Mark done</b> in the
            header.
          </p>
          {c.unreviewed > 0 ? (
            <button
              type="button"
              className={`${action.primary} ${styles.reviewBtn}`}
              onClick={review}
              data-debrief-review=""
            >
              <Eye size={16} aria-hidden="true" />
              Review {c.unreviewed} {c.unreviewed === 1 ? "item" : "items"}
            </button>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function FoundMeta({ item }: { item: DebriefItem }) {
  const bits = [];
  if (item.commits) bits.push(`${item.commits} ${item.commits === 1 ? "commit" : "commits"}`);
  if (item.pr) bits.push(`PR #${item.pr}`);
  if (!bits.length) bits.push(KIND_LABEL[item.kind] ?? item.kind);
  return <span className={styles.judged}>{bits.join(" · ")}</span>;
}

function OpenList({
  id,
  title,
  total,
  shown,
  hint,
  children,
}: {
  id: string;
  title: string;
  total: number;
  /** Rows the server listed (it lists at most 50 per list); the rest are counted, not dropped. */
  shown: number;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={styles.openList} data-debrief-open={id}>
      <div className={styles.openHead}>
        <span>{title}</span>
        <span className={styles.count} data-debrief-open-count="">
          {total}
        </span>
      </div>
      {hint ? <p className={styles.hint}>{hint}</p> : null}
      {total === 0 ? (
        <p className={styles.muted}>None</p>
      ) : (
        <>
          <ul className={styles.rows}>{children}</ul>
          {total > shown ? (
            <p className={styles.hint} data-debrief-open-more="">
              The first {shown} of {total}.
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}

function Sessions({
  facts,
  sessionLabel,
}: {
  facts: DebriefFacts;
  sessionLabel: (key: string | null) => string;
}) {
  return (
    <section>
      <h4 className={styles.lbl}>Sessions</h4>
      {facts.sessions.length === 0 ? (
        <p className={styles.muted}>No session joined this mission.</p>
      ) : (
        <ul className={styles.sessions} data-debrief-sessions="">
          {facts.sessions.map((s) => {
            // A session still on the roster has no end yet: only a finished one has a duration.
            const mins =
              s.added_at != null && s.removed_at != null
                ? Math.max(0, Math.round((s.removed_at - s.added_at) / 60))
                : null;
            return (
              <li key={s.key}>
                <span className={styles.eng}>{engineOf(s.key)}</span>
                {sessionLabel(s.key)}
                {mins != null ? <span className={styles.judged}> {mins} min</span> : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function OnTheShelf({
  facts,
  sessionLabel,
  groups,
}: {
  facts: DebriefFacts;
  sessionLabel: (key: string | null) => string;
  /** Without a summary the shelf's groups are named too (the structured debrief). */
  groups: boolean;
}) {
  const c = facts.shelf.counts;
  const fc = c.file_checks;
  return (
    <aside className={styles.shelfBox} data-debrief-shelf="">
      <h4 className={styles.lbl}>On the shelf</h4>
      <p className={styles.total}>
        <span className="nj-readout">{c.total}</span>
        <span className={styles.lbl}>{c.unreviewed} to review</span>
      </p>
      {c.total === 0 ? (
        <p className={styles.muted}>
          Nothing: no item was delivered, and git found no change since the sessions joined.
        </p>
      ) : (
        <>
          <dl className={styles.kinds}>
            {KIND_ORDER.filter((k) => c.by_kind[k]).map((k) => (
              <div key={k}>
                <dt>{KIND_LABEL[k]}</dt>
                <dd>{c.by_kind[k]}</dd>
              </div>
            ))}
          </dl>
          <p className={styles.lbl}>
            {c.by_source.delivered} delivered · {c.by_source.found} found in git
          </p>
          {groups ? (
            <ul className={styles.groups} data-debrief-groups="">
              {facts.shelf.groups.map((g) => (
                <li key={g.session_key ?? "-"} data-debrief-group={g.session_key ?? ""}>
                  <span className={styles.eng}>{engineOf(g.session_key) || "—"}</span>
                  {g.session_key ? sessionLabel(g.session_key) : "No session"}
                  <span className={styles.judged}>
                    {" "}
                    {g.total} {g.total === 1 ? "item" : "items"}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </>
      )}
      <div className={styles.checks} data-debrief-file-checks="">
        <h4 className={styles.lbl}>File checks</h4>
        {c.total === 0 ? (
          <p className={styles.muted}>Nothing delivered, so nothing to check.</p>
        ) : fc.changed + fc.missing + fc.inaccessible === 0 ? (
          <p className={styles.muted}>Every file is as delivered.</p>
        ) : (
          <>
            {fc.changed ? (
              <StateWord word={`${fc.changed} changed since delivered`} led="attention" tone="warn" />
            ) : null}
            {fc.missing + fc.inaccessible ? (
              <StateWord word={`${fc.missing + fc.inaccessible} missing`} led="idle" tone="idle" />
            ) : null}
          </>
        )}
      </div>
    </aside>
  );
}

export type { Debrief };
