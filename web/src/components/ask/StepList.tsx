/** HQ's step list (#2251): a multi-part request sorted into ordered steps, walked through one card
 *  at a time. It is a card like the others (the same chamfered plate and callsign head), drawn
 *  ONCE in the thread, under the newest turn that changed it. Every row's state is the server's,
 *  derived from the step's cards; the current step's card sits in its slot, drawn by the card
 *  components the thread uses everywhere else (`renderCard`), so its controls are the same.
 *
 *  Nothing here decides anything: "Discuss this step" only puts "Step N: " in the composer, and
 *  End steps posts the one route that dismisses the waiting card and ends the list. State is a
 *  word beside a mark, never colour alone. */
import type { ReactNode } from "react";

import type { AskStep, AskStepList } from "../../types/api";
import { relTime } from "../../lib/format";
import c from "./AskCards.module.css";
import s from "./StepList.module.css";

const pad = (n: number) => String(n).padStart(2, "0");

function word(step: AskStep, firstNext: boolean): string {
  switch (step.state) {
    case "now":
      return "Now";
    case "done":
      return step.started ? "Started" : "Done";
    case "held":
      return step.reason === "card" ? "Held" : "Waiting";
    case "skipped":
      return "Skipped";
    case "ended":
      return "Ended";
    default:
      return firstNext ? "Next" : "Later";
  }
}

function detail(step: AskStep, list: AskStepList): string {
  const when = step.settled_at ? ` ${relTime(step.settled_at)}` : "";
  if (step.state === "done" && step.started)
    return `Started and got its brief${when ? "," : ""}${when}. Its work is not tracked here.`;
  if (step.state === "done") return `${step.outcome || "Done."}${when ? " ·" : ""}${when}`;
  if (step.state === "held" && step.reason === "needs_skipped")
    return "It needs a step that was skipped.";
  if (step.state === "held") return step.outcome || "Its card did not complete. Nothing after it moves.";
  if (step.state === "skipped")
    return step.outcome ? `Skipped; ${step.outcome}.` : "Skipped. Nothing of it ran.";
  if (step.state === "ended" && list.status === "ended") return "";
  return step.detail;
}

function needsLine(step: AskStep, list: AskStepList): string {
  if (step.state !== "next" || step.needs.length === 0) return "";
  const named = step.needs
    .map((sid) => list.steps.find((x) => x.sid === sid))
    .filter((x): x is AskStep => Boolean(x))
    .map((x) => `step ${x.n}`);
  return named.length ? `Needs ${named.join(", ")}` : "";
}

function progress(list: AskStepList): string {
  const cur = list.steps.find((x) => x.sid === list.current);
  if (list.status === "finished") return "Finished";
  if (list.status === "ended") return "Ended";
  if (list.status === "held" && cur) return `Held at step ${cur.n}`;
  return cur ? `Step ${cur.n} of ${list.steps.length}` : `${list.steps.length} steps`;
}

export function StepList({
  list,
  renderCard,
  onDiscuss,
  onEnd,
  ending,
  error,
}: {
  list: AskStepList;
  /** The card with this id, drawn as the thread draws it (or null when it isn't loaded).
   *  `slot`: every card id the step shows, so a same-kind batch folds together (#1938). */
  renderCard: (id: string, slot: string[]) => ReactNode;
  onDiscuss: (step: AskStep) => void;
  onEnd: () => void;
  ending: boolean;
  error: string | null;
}) {
  const open = list.status === "active" || list.status === "held";
  const firstNext = list.steps.find((x) => x.state === "next")?.sid;
  const cur = list.steps.find((x) => x.sid === list.current);
  return (
    <section
      className={`${c.card} ${s.list}`}
      aria-label={`Steps: ${list.title}`}
      data-testid="ask-steps"
      data-status={list.status}
    >
      <div className={c.head}>
        <span className={c.k}>Steps</span>
        <span className={s.title}>{list.title}</span>
        <span className={s.prog} data-testid="ask-steps-progress">
          {progress(list)}
          <span className={s.pips} aria-hidden="true">
            {list.steps.map((x) => (
              <i key={x.sid} className={s[`pip_${x.state}`]} />
            ))}
          </span>
        </span>
      </div>
      <ol className={s.rows}>
        {list.steps.map((step) => {
          const slot =
            open && step.sid === list.current && step.cards.length > 0 ? step.cards : null;
          const more = detail(step, list);
          const needs = needsLine(step, list);
          return [
            <li
              key={step.sid}
              className={`${s.row} ${s[`row_${step.state}`] ?? ""}`}
              data-testid="ask-step"
              data-state={step.state}
              data-sid={step.sid}
            >
              <span className={s.n}>{pad(step.n)}</span>
              <span className={s.mk} aria-hidden="true" />
              <span className={s.body}>
                <span className={s.t}>{step.title}</span>
                {more ? <span className={s.d}>{more}</span> : null}
                {needs ? <span className={s.dep}>{needs}</span> : null}
              </span>
              <span className={s.w}>{word(step, step.sid === firstNext)}</span>
              {open && step.state === "next" ? (
                <button
                  type="button"
                  className={`${c.ghost} ${s.disc}`}
                  onClick={() => onDiscuss(step)}
                  data-testid="ask-step-discuss"
                >
                  Discuss this step
                </button>
              ) : null}
            </li>,
            slot ? (
              <li key={`${step.sid}:slot`} className={s.slot} data-testid="ask-step-slot">
                {slot.map((id) => (
                  <div key={id}>{renderCard(id, slot)}</div>
                ))}
              </li>
            ) : null,
          ];
        })}
      </ol>
      {open ? (
        <div className={s.foot}>
          <p>
            {list.status === "held" && cur
              ? `Step ${cur.n} is on hold. Tell me how to change it, skip it, or end the steps.`
              : "Only the card in front of you can run. Tell me to change, reorder, skip or add a step."}
          </p>
          <button
            type="button"
            className={c.ghost}
            onClick={onEnd}
            disabled={ending}
            data-testid="ask-steps-end"
          >
            End steps
          </button>
          {error ? (
            <p className={s.err} role="alert" data-testid="ask-steps-error">
              {error}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

/** Where a step list was first drawn before it moved under a newer turn (#2251 G6). */
export function StepsMoved() {
  return (
    <div className={s.moved} role="note" data-testid="ask-steps-moved">
      Steps changed · now below
    </div>
  );
}
