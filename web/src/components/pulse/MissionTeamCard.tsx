/** The mission's TEAM (#1950, #1613 P5; mockups `docs/mockups/mission-team-1950/`).
 *
 * HQ plans every mission with the Commander. This card shows the team on the latest plan revision:
 * for each agent step, the role it needs, the specialist assigned, the effective model and its
 * grade, the server's reason, up to three alternates and the notes (queueing, unknown usage). It
 * offers the same operations HQ has: **Assemble**, and **Change** per step (a specialist or a model
 * for this mission only, picked from the server's own list).
 *
 * **The client decides nothing.** Every reason, note, verdict and refusal is server text, re-derived
 * on each read (`GET …/plan/team`): a grade remapped in Settings shows as "no longer qualifies" on
 * the next read. A choice is sent as its INDEX in the list the server built, with that list's
 * `listing` digest; a disabled entry can't be chosen, and the server refuses it anyway. A refusal
 * writes nothing and is shown on its step. Nothing here approves or launches: that is P6.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useConfig } from "../../app/config";
import { ApiError, api } from "../../lib/api";
import { HQ_PATH, SPECIALISTS_PATH } from "../../lib/routes";
import type {
  MissionTeam,
  TeamChoices,
  TeamOp,
  TeamReason,
  TeamStep,
} from "../../types/missionTeam";
import type { Grade } from "../../types/specialists";
import { Disclosure } from "../hud/Controls";
import { GRADE_LABEL } from "../specialists/grades";
import action from "../ui/actionButton.module.css";
import styles from "./missionTeam.module.css";

/** The states a team may be planned in: before dispatch (the server enforces the same). */
const PLANNING = new Set(["draft", "planned"]);

type Refusal = { step: string | null; text: string; code: string };

function GradeChip({ grade }: { grade: Grade | null }) {
  if (!grade) return <span className={styles.ungraded}>Ungraded</span>;
  return <span className={styles.grade}>{GRADE_LABEL[grade]}</span>;
}

function newId(): string {
  return crypto.randomUUID();
}

function refusalOf(e: unknown, step: string | null): Refusal {
  if (e instanceof ApiError) {
    const body = (e.record ?? {}) as { code?: string; detail?: string };
    return { step, text: body.detail || e.message, code: body.code ?? "" };
  }
  return { step, text: "That did not go through. Nothing was written.", code: "" };
}

/** The server's refusal, ending in "Nothing was written." once: some refusals already say it. */
function said(text: string): string {
  const t = text.trim().replace(/\.$/, "");
  return /nothing was written/i.test(t) ? `${t}.` : `${t}. Nothing was written.`;
}

function Role({ step }: { step: TeamStep }) {
  if (!step.role) return null;
  return (
    <>
      {step.role.skills.map((s) => (
        <span key={s} className={styles.skill}>
          {s}
        </span>
      ))}
      {step.role.min_grade ? (
        <span className={styles.grade}>min {GRADE_LABEL[step.role.min_grade]}</span>
      ) : (
        <span className={styles.actor}>any grade</span>
      )}
      {step.distinct_from.length ? (
        <span className={styles.actor}>
          distinct from {step.distinct_from.map((d) => d.step.split(".")[1]).join(", ")}
        </span>
      ) : null}
    </>
  );
}

function Codes({ reasons }: { reasons: TeamReason[] }) {
  return <span className={styles.code}>{[...new Set(reasons.map((r) => r.code))].join(" · ")}</span>;
}

/** The per-step picker: specialists, then models for this mission only. One selection at a time. */
function Picker({
  missionId,
  step,
  onPick,
  onCancel,
}: {
  missionId: string;
  step: TeamStep;
  onPick: (op: TeamOp) => void;
  onCancel: () => void;
}) {
  const [lists, setLists] = useState<TeamChoices | null>(null);
  const [error, setError] = useState("");
  const [chosen, setChosen] = useState<{ kind: "assign" | "set_override"; index: number } | null>(
    null,
  );
  useEffect(() => {
    let live = true;
    api
      .missionTeamChoices(missionId, step.id)
      .then((r) => live && setLists(r))
      .catch((e: unknown) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [missionId, step.id]);
  const name = `team-pick-${step.id}`;
  const use = () => {
    if (!lists || !chosen) return;
    const listing =
      chosen.kind === "assign" ? lists.candidates.listing : lists.overrides.listing;
    onPick({ op: chosen.kind, step: step.id, index: chosen.index, listing });
  };
  const option = (
    kind: "assign" | "set_override",
    index: number,
    label: string,
    grade: Grade | null,
    ok: boolean,
    reasons: TeamReason[],
  ) => {
    const id = `${name}-${kind}-${index}`;
    const sel = chosen?.kind === kind && chosen.index === index;
    return (
      <li key={id} className={`${styles.opt} ${ok ? "" : styles.optOff} ${sel ? styles.optOn : ""}`}>
        <input
          id={id}
          type="radio"
          name={name}
          disabled={!ok}
          checked={sel}
          onChange={() => setChosen({ kind, index })}
          aria-describedby={ok ? undefined : `${id}-why`}
        />
        <label htmlFor={id} className={styles.oname}>
          {label}
        </label>
        <GradeChip grade={grade} />
        {ok ? null : (
          <span id={`${id}-why`} className={styles.oreason}>
            {reasons[0]?.text}
          </span>
        )}
      </li>
    );
  };
  return (
    <div className={styles.picker} data-testid="team-picker">
      <div className={styles.phead}>
        <b>{step.title}</b>
        <Role step={step} />
      </div>
      {error ? (
        <p className={styles.alert} role="alert">
          {error}
        </p>
      ) : !lists ? (
        <p className={styles.dim}>Reading the lists…</p>
      ) : (
        <>
          <div className={styles.ogroup}>Specialist</div>
          {lists.candidates.choices.length ? (
            <ul className={styles.opts}>
              {lists.candidates.choices.map((c) =>
                option(
                  "assign",
                  c.index,
                  `${c.specialist.id} v${c.specialist.version} · ${c.engine} · ${c.model ?? "default"}`,
                  c.grade,
                  c.ok,
                  c.reasons,
                ),
              )}
            </ul>
          ) : (
            <p className={styles.dim}>No specialists in your pool.</p>
          )}
          {step.assignment ? (
            <>
              <div className={styles.ogroup}>Model for this mission only</div>
              <ul className={styles.opts}>
                {lists.overrides.choices.map((m) =>
                  option(
                    "set_override",
                    m.index,
                    `${m.engine} · ${m.model}${m.effort ? ` · effort ${m.effort}` : ""}`,
                    m.grade,
                    m.ok,
                    m.reasons,
                  ),
                )}
              </ul>
            </>
          ) : null}
        </>
      )}
      <div className={styles.pfoot}>
        <span className={styles.hint}>You pick from this list. Nothing typed becomes a model.</span>
        {step.assignment?.override ? (
          <button
            type="button"
            className={`${action.ghost} ${styles.btn}`}
            onClick={() => onPick({ op: "clear_override", step: step.id })}
          >
            Clear override
          </button>
        ) : null}
        <button type="button" className={`${action.ghost} ${styles.btn}`} onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className={`${action.primary} ${styles.btn}`}
          disabled={!chosen}
          onClick={use}
        >
          Use this
        </button>
      </div>
    </div>
  );
}

function StepRow({
  step,
  editable,
  refusal,
  picking,
  onChange,
  picker,
}: {
  step: TeamStep;
  editable: boolean;
  refusal: Refusal | null;
  picking: boolean;
  onChange: () => void;
  picker: React.ReactNode;
}) {
  if (!step.assignable) {
    return (
      <li className={`${styles.step} ${styles.quiet}`} data-testid={`team-step-${step.id}`}>
        <div className={styles.sline}>
          <span className={styles.stitle}>{step.title}</span>
          <span className={styles.sid}>{step.id}</span>
          <span className={styles.actor}>{step.actor} step</span>
        </div>
      </li>
    );
  }
  const d = step.derived;
  const a = step.assignment;
  const stale = d && !d.ok;
  return (
    <li
      className={`${styles.step} ${stale || step.unassigned || refusal ? styles.warn : ""}`}
      data-testid={`team-step-${step.id}`}
    >
      <div className={styles.sline}>
        <span className={styles.stitle}>{step.title}</span>
        <span className={styles.sid}>{step.id}</span>
        <Role step={step} />
      </div>
      {editable ? (
        <div className={styles.side}>
          <button
            type="button"
            className={`${action.ghost} ${styles.btn}`}
            aria-expanded={picking}
            onClick={onChange}
          >
            Change
          </button>
        </div>
      ) : null}
      {refusal ? (
        <p className={styles.alert} role="alert" data-testid="team-refusal">
          <b>REFUSED</b>
          {said(refusal.text)} {refusal.code ? <span className={styles.code}>{refusal.code}</span> : null}
        </p>
      ) : null}
      {a && d ? (
        <>
          <div className={styles.who}>
            <span className={styles.spec}>{a.specialist.id}</span>
            <span className={styles.ver}>v{a.specialist.version}</span>
            <span className={styles.runs}>
              {d.effective.engine} · {d.effective.model ?? "default"}
              {d.effective.effort ? ` · ${d.effective.effort}` : ""}
            </span>
            <GradeChip grade={d.grade} />
            {a.override ? <span className={styles.ovr}>OVERRIDE · THIS MISSION</span> : null}
          </div>
          {stale ? (
            <p className={styles.gone} data-testid="team-stale">
              <b>NO LONGER QUALIFIES</b>
              {d.reasons.map((r) => r.text).join("; ")}. Approval will refuse this step until you
              change it. <Codes reasons={d.reasons} />
            </p>
          ) : null}
          <p className={styles.why}>{a.reason}</p>
          {d.notes.length ? (
            <div className={styles.notes}>
              {d.notes.map((n) => (
                <span
                  key={n.code}
                  className={`${styles.note} ${n.code === "usage_unknown" ? styles.noteInfo : ""}`}
                  title={n.code === "usage_unknown" ? n.text : undefined}
                >
                  {n.code === "usage_unknown" ? "usage unknown" : n.text}
                </span>
              ))}
            </div>
          ) : null}
          <Disclosure
            className={styles.alt}
            summary={<span className={styles.altSummary}>Alternates ({a.alternates.length})</span>}
          >
            {a.alternates.length ? (
              <ol className={styles.altList}>
                {a.alternates.map((x) => (
                  <li key={x.specialist.id}>
                    <b>{x.specialist.id}</b> v{x.specialist.version} — {x.reason}
                  </li>
                ))}
              </ol>
            ) : (
              <p className={styles.dim}>No other specialist qualified.</p>
            )}
          </Disclosure>
        </>
      ) : step.unassigned ? (
        <>
          <p className={styles.gone} data-testid="team-unassigned">
            <b>UNASSIGNED</b>
            {step.unassigned.summary.charAt(0).toUpperCase() + step.unassigned.summary.slice(1)}.
          </p>
          {step.unassigned.candidates.length ? (
            <Disclosure
              className={styles.alt}
              summary={
                <span className={styles.altSummary}>
                  Why each one was refused ({step.unassigned.candidates.length})
                </span>
              }
            >
              <ol className={styles.altList}>
                {step.unassigned.candidates.map((c) => (
                  <li key={c.specialist}>
                    <b>{c.specialist}</b> — {c.reasons.map((r) => r.text).join("; ")}.{" "}
                    <Codes reasons={c.reasons} />
                  </li>
                ))}
              </ol>
            </Disclosure>
          ) : null}
        </>
      ) : null}
      {picking ? picker : null}
    </li>
  );
}

export function MissionTeamCard({
  missionId,
  state,
  onWrite,
}: {
  missionId: string;
  state: string;
  /** Called after every team write, so the plan-approval card (#1951) re-reads. */
  onWrite?: () => void;
}) {
  const cfg = useConfig();
  const [data, setData] = useState<MissionTeam | null>(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  // Bumped after every write: the effect below re-reads the team. The parent keys this card by
  // mission, so another mission is a fresh card, never this one's state.
  const [reads, setReads] = useState(0);

  useEffect(() => {
    let alive = true;
    api
      .missionTeam(missionId)
      .then((r) => {
        if (!alive) return;
        setData(r);
        setLoadError("");
      })
      .catch((e: unknown) => {
        if (alive) setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [missionId, reads]);

  const revision = data?.revision ?? null;
  const run = useCallback(
    async (op: TeamOp, step: string | null) => {
      if (!revision) return;
      setBusy(true);
      setRefusal(null);
      try {
        await api.missionTeamOp(missionId, {
          ...op,
          operation_id: newId(),
          expected_revision: revision,
          reason:
            op.op === "assemble"
              ? "assembled from the pool"
              : op.op === "clear_override"
                ? "override cleared by the Commander"
                : "changed by the Commander",
        });
        setPicking(null);
      } catch (e) {
        setRefusal(refusalOf(e, step));
      } finally {
        setBusy(false);
        setReads((n) => n + 1);
        onWrite?.();
      }
    },
    [revision, missionId, onWrite],
  );

  if (loadError) {
    return (
      <section className={styles.team} aria-label="Team">
        <p className={styles.alert} role="alert">
          The team could not be read: {loadError}
        </p>
      </section>
    );
  }
  if (!data || !data.team || data.revision === null) return null;
  const team = data.team;
  const editable = PLANNING.has(state);
  const agentSteps = team.steps.filter((s) => s.assignable);
  if (!agentSteps.length) return null;
  const open = agentSteps.filter((s) => !s.assignment).length;
  const specialists = new Set(agentSteps.map((s) => s.assignment?.specialist.id).filter(Boolean));
  const hq = cfg?.pulse?.configured ?? false;
  const assembleWhy = !editable
    ? "The team is planned before the mission is dispatched."
    : team.pool_size === 0
      ? "Your pool has no specialists yet."
      : open === 0
        ? "Every agent step already has a specialist."
        : "";
  const skills = [...new Set(agentSteps.flatMap((s) => s.role?.skills ?? []))];
  return (
    <section className={styles.team} aria-label="Team" data-testid="mission-team">
      <div className={styles.thead}>
        <span className={styles.ttl}>Team</span>
        <span className={styles.rev}>
          revision {data.revision}
          {data.author_kind === "hq" ? " · by HQ, confirmed by you" : ""}
        </span>
        <div className={styles.tacts}>
          {hq ? (
            <Link className={`${action.ghost} ${styles.btn}`} to={HQ_PATH}>
              Ask HQ
            </Link>
          ) : (
            <button
              type="button"
              className={`${action.ghost} ${styles.btn}`}
              disabled
              aria-describedby="team-hq-why"
            >
              Ask HQ
            </button>
          )}
          <button
            type="button"
            className={`${action.primary} ${styles.btn}`}
            disabled={busy || !!assembleWhy}
            aria-describedby={assembleWhy ? "team-assemble-why" : undefined}
            onClick={() => void run({ op: "assemble" }, null)}
          >
            Assemble
          </button>
        </div>
      </div>
      {!hq ? (
        <p id="team-hq-why" className={styles.tsum}>
          HQ is not set up: it has no AI endpoint (Settings → HQ). Assemble, Change and the model
          list work without it: the team is chosen by the server, not by a model.
        </p>
      ) : null}
      {team.pool_size === 0 ? (
        <div className={styles.empty} data-testid="team-empty-pool">
          <p>
            <b>No specialists in your pool yet.</b> {agentSteps.length} steps need one
            {skills.length ? `: ${skills.join(", ")}` : ""}.
          </p>
          <p>Add a specialist with those skills, graded at least as each step needs, then Assemble.</p>
          <Link className={`${action.ghost} ${styles.btn}`} to={SPECIALISTS_PATH}>
            Open Specialists
          </Link>
        </div>
      ) : null}
      {assembleWhy && team.pool_size !== 0 ? (
        <p id="team-assemble-why" className={styles.dim}>
          {assembleWhy}
        </p>
      ) : assembleWhy && team.pool_size === 0 ? (
        <span id="team-assemble-why" hidden>
          {assembleWhy}
        </span>
      ) : null}
      <p className={styles.tsum}>
        {agentSteps.length} agent steps · {specialists.size} specialists · peak {team.peak} at once
        (pool limit {team.pool_max}). Nothing launches here: approval and Start come later.
        {team.pool_note ? ` Pool: ${team.pool_note}.` : ""}
      </p>
      {refusal && refusal.step === null ? (
        <p className={styles.alert} role="alert" data-testid="team-refusal">
          <b>REFUSED</b>
          {said(refusal.text)} {refusal.code ? <span className={styles.code}>{refusal.code}</span> : null}
        </p>
      ) : null}
      <ul className={styles.steps}>
        {team.steps.map((s) => (
          <StepRow
            key={s.id}
            step={s}
            editable={editable && !busy}
            refusal={refusal && refusal.step === s.id ? refusal : null}
            picking={picking === s.id}
            onChange={() => {
              setRefusal(null);
              setPicking(picking === s.id ? null : s.id);
            }}
            picker={
              <Picker
                missionId={missionId}
                step={s}
                onCancel={() => setPicking(null)}
                onPick={(op) => void run(op, s.id)}
              />
            }
          />
        ))}
      </ul>
    </section>
  );
}
