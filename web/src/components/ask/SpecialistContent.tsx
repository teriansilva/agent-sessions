/** A specialist card's content (#1938): the specialist in full as hairline rows in a fixed order,
 *  an edit as a per-field diff (− before, + after; changed rows first), the specialists a playbook
 *  card relies on, and the one-row fold several specialist cards of a turn collapse to.
 *
 *  Everything is text. The grade is a chip plus a fixed hint and never an input: it follows the
 *  model, from the operator's model grades. A sign (−, +, !) always carries what a tone repeats. */
import { ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "../../lib/api";
import type { AskLibraryContent, AskReliesRow, AskSpecialistShown } from "../../types/api";
import { Led, type LedTone } from "../hud/Meters";
import c from "./AskCards.module.css";
import {
  CREATE_ROWS,
  FIELD_LABEL,
  GRADE_HINT,
  type EditRow,
  type SkillLine,
  type SpecialistField,
  agentText,
  capacityLine,
  editCounts,
  editRows,
  fieldText,
  gradeText,
  modelText,
  reliesCounts,
  reliesDetail,
  reliesWord,
  skillLines,
  skillRev,
} from "./specialistRows";

type Specialist = Extract<AskLibraryContent, { kind: "specialist" }>;

const SIGN_CLASS: Record<SkillLine["sign"], string> = { " ": c.specKeep, "+": c.add, "−": c.del };

function Skill({ k, signed }: { k: SkillLine; signed: boolean }) {
  // A pin that moved since the card was made: `!` and what the Library holds now.
  const warn = k.now !== undefined;
  const sign = warn ? "!" : k.sign;
  return (
    <li
      className={warn ? c.specWarn : signed ? SIGN_CLASS[k.sign] : ""}
      data-testid="ask-specialist-skill"
      data-moved={warn ? "true" : undefined}
    >
      {signed || warn ? <span className={c.specSign}>{sign}</span> : null}
      {k.id} <span className={c.specRev}>· {skillRev(k)}</span>
    </li>
  );
}

function Skills({ lines, signed }: { lines: SkillLine[]; signed: boolean }) {
  if (!lines.length) return <span>No skills</span>;
  return (
    <ul className={c.specSkills} aria-label="Skills">
      {lines.map((k) => (
        <Skill key={`${k.sign}${k.id}`} k={k} signed={signed} />
      ))}
    </ul>
  );
}

function Grade({ s }: { s: AskSpecialistShown }) {
  return (
    <span className={s.grade ? c.specGrade : c.specUngraded} data-testid="ask-specialist-grade">
      {gradeText(s)}
    </span>
  );
}

function Value({
  s,
  field,
  now,
}: {
  s: AskSpecialistShown;
  field: SpecialistField;
  now: Specialist["skills_now"];
}) {
  if (field === "grade")
    return (
      <>
        <Grade s={s} />
        <span className={c.specHint}>{GRADE_HINT}</span>
      </>
    );
  if (field === "skills") return <Skills lines={skillLines(s, now)} signed={false} />;
  if (field === "capacity") return <b className={c.specCap}>{capacityLine(s.capacity)}</b>;
  return <>{fieldText(s, field)}</>;
}

function Changed({ row }: { row: EditRow }) {
  if (row.field === "skills") return <Skills lines={row.skills ?? []} signed />;
  return (
    <>
      <div className={`${c.specD} ${c.del}`}>
        <span className={c.specSign}>−</span>
        {row.before}
      </div>
      <div className={`${c.specD} ${c.add}`}>
        <span className={c.specSign}>+</span>
        {row.after}
      </div>
      {row.field === "grade" ? <span className={c.specHint}>{GRADE_HINT}</span> : null}
    </>
  );
}

/** `stale`: the card is out of date, so these are the values it was made with, kept as they were. */
export function SpecialistContent({ content, stale }: { content: Specialist; stale: boolean }) {
  const { after, before, skills_now: now } = content;
  const staged = stale ? (
    <p className={c.staged} data-testid="ask-specialist-staged">
      <b>
        {before && content.version
          ? `As proposed against version ${content.version.from}. `
          : "As proposed. "}
      </b>
      These are the values this card was made with, kept as they were. Ask HQ to propose it again.
    </p>
  ) : null;
  if (!before) {
    return (
      <div data-testid="ask-specialist">
        {staged}
        <div className={c.blockLabel}>The specialist, in full</div>
        <dl className={c.spec} data-testid="ask-specialist-rows">
          {CREATE_ROWS.map((field) => (
            <div
              key={field}
              className={c.specRow}
              data-field={field}
              data-testid="ask-specialist-row"
            >
              <dt>{FIELD_LABEL[field]}</dt>
              <dd>
                <Value s={after} field={field} now={now} />
              </dd>
            </div>
          ))}
        </dl>
      </div>
    );
  }
  const rows = editRows(before, after, now);
  const n = editCounts(rows);
  return (
    <div data-testid="ask-specialist">
      {staged}
      <div className={c.specCounts} data-testid="ask-specialist-counts">
        <span className={c.teamLbl}>Changes</span>
        <span>
          <b>{n.changed}</b> changed
        </span>
        <span>
          <b>{n.follows}</b> follows
        </span>
        <span>
          <b>{n.unchanged}</b> unchanged
        </span>
      </div>
      <dl className={c.spec} data-testid="ask-specialist-rows">
        {rows.map((row) => (
          <div
            key={row.field}
            className={`${c.specRow} ${row.state === "same" ? c.specSame : c.specChg}`}
            data-field={row.field}
            data-state={row.state}
            data-testid="ask-specialist-row"
          >
            <dt>{FIELD_LABEL[row.field]}</dt>
            <dd>
              {row.state === "same" ? (
                <Value s={after} field={row.field} now={now} />
              ) : (
                <Changed row={row} />
              )}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

const RELIES_CLASS: Record<AskReliesRow["state"], string> = {
  proposed: c.teamS_assembled,
  exists: c.teamS_kept,
  missing: c.teamS_unassigned,
};

/** The specialists a playbook card says it relies on, with their state at this read. A state is
 *  a marker plus a word; none of them blocks saving the playbook. */
export function ReliesList({ rows }: { rows: AskReliesRow[] }) {
  const n = reliesCounts(rows);
  return (
    <div className={c.relies} data-testid="ask-relies">
      <div className={c.teamHead} data-testid="ask-relies-counts">
        <span className={c.teamLbl}>Specialists it relies on</span>
        <span className={c.teamCnt}>
          <b>{n.named}</b> named
        </span>
        <span className={c.teamCnt}>
          <b>{n.exists}</b> {n.exists === 1 ? "exists" : "exist"}
        </span>
        <span className={c.teamCnt}>
          <b>{n.proposed}</b> proposed
        </span>
        <span className={c.teamCnt}>
          <b>{n.missing}</b> missing
        </span>
      </div>
      <ol className={c.team}>
        {rows.map((r, i) => (
          <li key={r.title} className={c.teamRow} data-testid="ask-relies-row" data-state={r.state}>
            <span className={c.teamN}>{String(i + 1).padStart(2, "0")}</span>
            <span className={`${c.teamState} ${RELIES_CLASS[r.state]}`}>
              {r.state === "missing" ? (
                <Led tone="att" />
              ) : (
                <span className={c.teamMk} aria-hidden="true" />
              )}
              {reliesWord(r)}
            </span>
            <span className={`${c.teamStep} ${c.reliesName}`}>
              {r.title}
              {r.state === "exists" ? <span className={c.teamVer}>version {r.version}</span> : null}
            </span>
            <span className={c.teamPick}>
              <span className={c.teamWhy}>{reliesDetail(r)}</span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

export const RELIES_NOTE =
  "Saving the playbook creates no specialist. A playbook names roles; the mission's team is " +
  "assembled later.";

/** A folded specialist card: one full-width button with its ordinal, title, what it runs on and
 *  its capacity. It carries no Confirm: that is offered only once the card is open and its
 *  content is on screen. A decided card shows its outcome's LED and word in place of Review. */
export function FoldRow({
  id,
  ordinal,
  title,
  open,
  onToggle,
  outcome,
  controls,
}: {
  id: string;
  ordinal: number;
  title: string;
  open: boolean;
  onToggle: () => void;
  outcome: { word: string; led: LedTone; tone: string } | null;
  controls: string;
}) {
  // What it runs on and its capacity come from the card's own content; the row is useful without
  // them, so a failed read leaves them out rather than blocking the row.
  const [shown, setShown] = useState<AskSpecialistShown | null>(null);
  useEffect(() => {
    let live = true;
    api.askProposalContent(id).then(
      (body) => {
        if (live && body.kind === "specialist") setShown(body.after);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [id]);
  return (
    <button
      type="button"
      className={c.foldRow}
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
      data-testid="ask-specialist-fold"
    >
      <span className={c.foldN}>{String(ordinal).padStart(2, "0")}</span>
      <ChevronRight className={c.foldChevron} size={14} aria-hidden="true" />
      <span className={c.foldTitle}>{title}</span>
      <span className={c.foldMeta} data-testid="ask-specialist-fold-meta">
        {shown ? `${agentText(shown)} · ${modelText(shown)}` : ""}
      </span>
      <span className={c.foldCap}>{shown ? capacityLine(shown.capacity) : ""}</span>
      <span className={c.foldGo} data-testid="ask-specialist-fold-go">
        {open ? (
          "Fold"
        ) : outcome ? (
          <span className={c.foldOutcome}>
            <Led tone={outcome.led} />
            <span className={`${c.ow} ${c[`ow_${outcome.tone}`]}`}>{outcome.word}</span>
          </span>
        ) : (
          "Review"
        )}
      </span>
    </button>
  );
}
