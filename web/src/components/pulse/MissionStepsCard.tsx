/** THE RUNNING BOARD (#1581 S4a; mockups `docs/mockups/mission-steps-1581/`).
 *
 * Each agent step of the plan runs in its own API session. The board lists every step with its
 * derived state and its runs, and offers the operator's own taps: **Start step** (the revision and
 * the folder the board showed are what is sent; the server re-checks both), **Stop step** (a
 * requested stop: the step is `stopped` only when the server proved its session gone), and
 * **Redirect** (one message into a running step's session, through the session's own route). The
 * budget meter shows turns and time; a `paused: budget` hold offers to raise the cap.
 *
 * **Under a plan approval** (#1951 slice B; mockups `docs/mockups/plan-approval-1951-b/`): HQ's
 * runner starts the ready steps (the head says so, each run says who started it), a launch the
 * approval refused shows as **refused** with old → new, and **Ask me before this step starts**
 * holds one step for the operator's own Start.
 *
 * **The client decides nothing.** Every state, reason and number is server text from
 * `GET …/steps`. Nothing here starts anything on its own: HQ never sees a Start.
 */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../../lib/api";
import { sessionPathFromKey } from "../../lib/format";
import type {
  MissionBudget,
  MissionStep,
  MissionSteps,
  StepRefusal,
  StepRun,
} from "../../types/missionSteps";
import action from "../ui/actionButton.module.css";
import styles from "./missionSteps.module.css";

const OPEN = new Set(["claimed", "running", "unknown"]);
/** How often the board re-reads while a step runs (the supervisor settles done steps). */
const POLL_MS = 5000;
/** How often an idle board re-reads: a plan or a team written elsewhere (HQ, another tab) shows up,
 *  and a first read that failed is retried (Hermes on #2007). */
const IDLE_POLL_MS = 15000;

function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const body = (e.record ?? {}) as { detail?: string };
    return body.detail || e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

function hours(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h ? `${h} h ${m} m` : `${m} m`;
}

function shortKey(key: string): string {
  const [engine, id = ""] = key.split(":");
  return id.length > 12 ? `${engine}:${id.slice(0, 4)}…${id.slice(-4)}` : key;
}

function stateOf(step: MissionStep): {
  word: string;
  tone: "live" | "warn" | "";
} {
  const last = step.runs[step.runs.length - 1];
  if (last && OPEN.has(last.state)) {
    return {
      word: last.state,
      tone: last.state === "unknown" ? "warn" : "live",
    };
  }
  // A launch the plan approval refused before anything was claimed (#1951 slice B).
  if (step.refusal && step.derived !== "done")
    return { word: "refused", tone: "warn" };
  if (last && (last.state === "refused" || last.state === "failed")) {
    if (step.derived !== "done") return { word: last.state, tone: "warn" };
  }
  if (step.derived === "done") return { word: "done", tone: "live" };
  if (step.hold_text) return { word: "held", tone: "" };
  if (step.derived === "failed") return { word: "failed", tone: "warn" };
  return { word: step.derived, tone: "" };
}

function RunLine({ run }: { run: StepRun }) {
  const path = run.session_key ? sessionPathFromKey(run.session_key) : null;
  return (
    <p className={styles.why} data-testid="step-run">
      <b>
        {run.specialist.id} v{run.specialist.version}
      </b>
      {run.model ? ` · ${run.model}` : ""} · episode {run.episode}
      {run.session_key ? " · " : ""}
      {run.session_key && path ? (
        <Link className={styles.sess} to={path}>
          {shortKey(run.session_key)}
        </Link>
      ) : run.session_key ? (
        <span className={styles.sess}>{shortKey(run.session_key)}</span>
      ) : null}
      {run.use_kind === "alternate" && run.use_reason
        ? ` · alternate: ${run.use_reason}`
        : ""}
      {run.started_by ? ` · ${run.started_by}` : ""}
      {run.detail ? ` · ${run.detail}` : ""}
    </p>
  );
}

function shown(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (Array.isArray(v)) return v.map(shown).join(", ");
  if (typeof v === "object") {
    const o = v as { model?: unknown };
    return o.model ? String(o.model) : JSON.stringify(v);
  }
  return String(v);
}

function clock(at: number): string {
  return new Date(at * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** A refused launch's one decision on its step (frame 7): what changed, old → new, its code. */
function RefusalLine({ refusal }: { refusal: StepRefusal }) {
  return (
    <div className={styles.why} data-testid="step-refusal">
      <p className={styles.rtext}>
        Checked at {refusal.boundary} {clock(refusal.at)}. {refusal.text}
      </p>
      <ul className={styles.changes}>
        {refusal.reasons.map((r, i) => (
          <li key={`${r.code}-${i}`}>
            {r.old !== undefined || r.new !== undefined ? (
              <>
                <span className={styles.sr}>changed from </span>
                <s>{shown(r.old)}</s>
                <span className={styles.arrow} aria-hidden="true">
                  {" → "}
                </span>
                <span className={styles.sr}>changed to </span>
                {shown(r.new)}
                {": "}
              </>
            ) : null}
            {r.text} <span className={styles.code}>{r.code}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function BudgetMeter({
  budget,
  busy,
  onRaise,
}: {
  budget: MissionBudget;
  busy: boolean;
  onRaise: () => void;
}) {
  const t = budget.turns;
  const pct = Math.min(100, Math.round((t.used / Math.max(1, t.cap)) * 100));
  const timePct = Math.min(
    100,
    Math.round((budget.seconds.used / Math.max(1, budget.seconds.cap)) * 100),
  );
  const shown = Math.max(pct, timePct);
  return (
    <>
      {budget.hold === "budget" ? (
        <p className={styles.alert} role="status" data-testid="budget-hold">
          <b>PAUSED: BUDGET</b>
          {budget.hold_detail ? `${budget.hold_detail}. ` : ""}No new step or
          automatic turn starts. Raise the cap or stop the mission. Once you
          raise it, automatic nudges resume; steps still start only on your tap.
        </p>
      ) : null}
      <div className={styles.meter} data-testid="budget-meter">
        <span className={styles.mk}>Budget</span>
        <span className={styles.bar} aria-hidden="true">
          <span
            className={`${styles.fill} ${shown >= 100 ? styles.hot : ""}`}
            style={{ width: `${shown}%` }}
          />
        </span>
        <span className={styles.mn}>
          {t.used} / {t.cap} turns · {hours(budget.seconds.used)} /{" "}
          {hours(budget.seconds.cap)}
        </span>
        <p className={styles.est}>
          {budget.estimated
            ? `Estimated: ${budget.units}, not tokens. These clients report no usage on their turns.`
            : budget.units}
        </p>
      </div>
      {budget.hold === "budget" ? (
        <div className={styles.side}>
          <button
            type="button"
            className={`${action.primary} ${styles.btn}`}
            disabled={busy}
            onClick={onRaise}
          >
            Raise to {budget.offer.turns_cap} turns
            {budget.seconds.used >= budget.seconds.cap
              ? ` and ${hours(budget.offer.seconds_cap)}`
              : ""}
          </button>
        </div>
      ) : null}
    </>
  );
}

type Pending =
  | { kind: "stop"; step: MissionStep; run: StepRun }
  | {
      kind: "redirect";
      step: MissionStep;
      run: StepRun;
      op: string;
      /** The text the op id was last tried with: an edited message is a new message. */
      tried?: string;
    }
  | { kind: "pause" }
  | { kind: "stopall" };

export function MissionStepsCard({
  missionId,
  refresh = 0,
}: {
  missionId: string;
  /** Bumped by the parent after a plan or team write: the board re-reads at once. */
  refresh?: number;
}) {
  const [data, setData] = useState<MissionSteps | null>(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [reads, setReads] = useState(0);
  const [pending, setPending] = useState<Pending | null>(null);
  const [text, setText] = useState("");
  const [interrupt, setInterrupt] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const label = useId();

  useEffect(() => {
    let alive = true;
    api
      .missionSteps(missionId)
      .then((r) => {
        if (!alive) return;
        setData(r);
        setLoadError("");
      })
      // A failed POLL keeps the last board (and any open confirmation): only a first read that
      // fails leaves the card empty.
      .catch((e: unknown) => alive && setLoadError(errorText(e)));
    return () => {
      alive = false;
    };
  }, [missionId, reads, refresh]);

  const anyOpen = !!data?.steps?.some((s) =>
    s.runs.some((r) => OPEN.has(r.state)),
  );
  useEffect(() => {
    const t = window.setInterval(
      () => setReads((n) => n + 1),
      anyOpen ? POLL_MS : IDLE_POLL_MS,
    );
    return () => window.clearInterval(t);
  }, [anyOpen]);

  useEffect(() => {
    const d = dialog.current;
    if (!d) return;
    if (pending && !d.open) {
      setError(""); // a board error from before is not this dialog's
      d.showModal();
    }
    if (!pending && d.open) d.close();
  }, [pending]);

  const act = useCallback(async (fn: () => Promise<string | void>) => {
    setBusy(true);
    setError("");
    setNote("");
    try {
      const said = await fn();
      if (said) setNote(said);
      setPending(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      setReads((n) => n + 1);
    }
  }, []);

  // An unreadable board shows nothing rather than a second error beside the plan's own cards: it
  // offers no action then, and every action re-reads the server's answer anyway.
  if (loadError && !data) return null;
  // A shape that is not a board (an older server, a mocked catch-all) renders nothing.
  if (!data || typeof data.revision !== "number" || !Array.isArray(data.steps))
    return null;
  const agent = data.steps.filter((s) => s.actor === "agent");
  if (!agent.length) return null;
  const assigned = agent.filter((s) => s.assignment !== null).length;
  const conc = data.concurrency;

  return (
    <section
      className={styles.board}
      aria-label="Steps"
      data-testid="mission-steps"
    >
      <div className={styles.thead}>
        <span className={styles.ttl}>Steps</span>
        <span className={styles.rev}>
          revision {data.revision}
          {conc ? ` · ${conc.open} of ${conc.cap} running` : ""}
        </span>
        {data.approval && data.runner ? (
          <span className={styles.auth} data-testid="steps-under-approval">
            under your approval
          </span>
        ) : null}
      </div>
      {data.runner ? (
        <p className={styles.tsum} data-testid="steps-runner">
          {data.runner} Every tool request still asks you.
        </p>
      ) : (
        <p className={styles.tsum}>
          Each agent step runs in its own API session, started by you. At most{" "}
          {conc?.cap ?? 2} at once; steps that share a folder take turns. Every
          tool request still asks you.
        </p>
      )}
      {data.budget ? (
        <div className={styles.side} data-testid="steps-controls">
          {data.budget.hold === "operator" || data.budget.stopping ? (
            <p
              className={styles.alert}
              role="status"
              data-testid="steps-paused"
            >
              <b>{data.budget.stopping ? "STOPPING" : "PAUSED"}</b>
              {data.budget.stopping
                ? "By you. Every open step is stopped as soon as it can be, and nothing new starts. Continue cancels the stop."
                : data.runner
                  ? "By you. No new step or automatic turn starts. Continue lets automatic nudges resume, and HQ starts ready steps again under your approval."
                  : "By you. No new step or automatic turn starts. Continue lets automatic nudges resume; steps still start only on your tap."}
            </p>
          ) : null}
          {data.budget.hold === "operator" || data.budget.stopping ? (
            <button
              type="button"
              className={`${action.primary} ${styles.btn}`}
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  await api.continueSteps(missionId);
                })
              }
            >
              Continue
            </button>
          ) : data.budget.hold === null ? (
            <button
              type="button"
              className={`${action.ghost} ${styles.btn}`}
              disabled={busy}
              onClick={() => {
                setInterrupt(false);
                setPending({ kind: "pause" });
              }}
            >
              Pause new steps
            </button>
          ) : null}
          <button
            type="button"
            className={`${action.ghost} ${styles.btn}`}
            disabled={busy}
            onClick={() => setPending({ kind: "stopall" })}
          >
            Stop mission…
          </button>
        </div>
      ) : null}
      {data.budget ? (
        <BudgetMeter
          budget={data.budget}
          busy={busy}
          onRaise={() => {
            const b = data.budget as MissionBudget;
            void act(async () => {
              await api.setMissionBudget(missionId, {
                turns_cap: b.offer.turns_cap,
                seconds_cap: b.offer.seconds_cap,
                expect_revision: b.revision,
              });
            });
          }}
        />
      ) : null}
      {error && !pending ? (
        <p className={styles.alert} role="alert" data-testid="steps-error">
          <b>REFUSED</b>
          {error}
        </p>
      ) : null}
      {note ? (
        <p className={styles.tsum} role="status" data-testid="steps-note">
          {note}
        </p>
      ) : null}
      {data.steps
        .filter((s) => s.refusal && s.derived !== "done")
        .map((s) => (
          <p
            key={`refused-${s.id}`}
            className={styles.alert}
            role="status"
            data-testid="steps-refused"
          >
            <b>REFUSED</b>
            {s.id}: {(s.refusal as StepRefusal).text}{" "}
            {(s.refusal as StepRefusal).remedy ?? ""}
          </p>
        ))}
      {assigned === 0 ? (
        <p className={styles.tsum} data-testid="steps-empty">
          <b>No agent step can start yet.</b> {agent.length} agent step
          {agent.length === 1 ? " has" : "s have"} no specialist: assemble the
          team first. Operator steps never start an agent; they wait for you.
        </p>
      ) : null}
      <ul className={styles.steps}>
        {data.steps.map((s) => {
          const st = stateOf(s);
          const last = s.runs[s.runs.length - 1];
          const lastOpen = last && OPEN.has(last.state);
          const rowTone =
            st.tone === "live" && lastOpen
              ? styles.live
              : st.tone === "warn"
                ? styles.bad
                : "";
          return (
            <li
              key={s.id}
              className={`${styles.step} ${rowTone} ${s.actor !== "agent" ? styles.quiet : ""}`}
              data-testid={`step-${s.id}`}
            >
              <div className={styles.sline}>
                <span className={styles.stitle}>{s.title}</span>
                <span className={styles.sid}>{s.id}</span>
                {s.actor !== "agent" ? (
                  <span className={styles.sid}>{s.actor}</span>
                ) : null}
                <span
                  className={`${styles.st} ${st.tone === "live" ? styles.stLive : st.tone === "warn" ? styles.stWarn : ""}`}
                  data-testid="step-state"
                >
                  {st.word}
                </span>
              </div>
              {s.actor === "agent" ? (
                <div className={styles.side}>
                  {last && last.state === "running" && last.session_key ? (
                    <button
                      type="button"
                      className={`${action.ghost} ${styles.btn}`}
                      disabled={busy}
                      onClick={() => {
                        setText("");
                        setPending({
                          kind: "redirect",
                          step: s,
                          run: last,
                          // One id per message: a retry after an unclear failure observes the
                          // first send instead of sending twice.
                          op: crypto.randomUUID(),
                        });
                      }}
                    >
                      Redirect…
                    </button>
                  ) : null}
                  {last &&
                  (last.state === "running" || last.state === "unknown") ? (
                    <button
                      type="button"
                      className={`${action.ghost} ${styles.btn}`}
                      disabled={busy}
                      onClick={() =>
                        setPending({ kind: "stop", step: s, run: last })
                      }
                    >
                      Stop step…
                    </button>
                  ) : null}
                  {s.derived === "ready" && !lastOpen ? (
                    <button
                      type="button"
                      className={`${action.primary} ${styles.btn}`}
                      disabled={busy || !s.can_start || !s.cwd}
                      onClick={() =>
                        void act(async () => {
                          const out = await api.startStep(missionId, s.id, {
                            revision: data.revision as number,
                            expect_cwd: s.cwd as string,
                          });
                          if (out.outcome !== "started") {
                            throw new Error(
                              out.reason || `the step was ${out.outcome}`,
                            );
                          }
                        })
                      }
                    >
                      Start step
                    </button>
                  ) : null}
                </div>
              ) : null}
              {last ? <RunLine run={last} /> : null}
              {s.refusal && s.derived !== "done" ? (
                <RefusalLine refusal={s.refusal} />
              ) : null}
              {s.runner_text ? (
                <p className={styles.why} data-testid="step-runner-done">
                  {s.runner_text}
                </p>
              ) : null}
              {s.hold_text ? (
                <p className={styles.why} data-testid="step-held">
                  {s.hold_text}
                </p>
              ) : null}
              {s.actor === "agent" &&
              s.why_not &&
              !lastOpen &&
              s.derived !== "done" ? (
                <p className={styles.why} data-testid="step-why">
                  {s.why_not === "waiting for an earlier step" && s.after.length
                    ? `Waits for ${s.after.join(" and ")}.`
                    : s.why_not.startsWith("waiting") || s.derived !== "ready"
                      ? capital(s.why_not)
                      : `${capital(s.why_not)}: Start becomes available when that changes.`}
                </p>
              ) : null}
              {s.actor === "agent" &&
              s.cwd &&
              s.derived === "ready" &&
              !lastOpen ? (
                <p className={styles.why}>in {s.cwd}</p>
              ) : null}
              {s.actor === "agent" &&
              data.approval &&
              s.derived !== "done" &&
              !lastOpen ? (
                <label className={styles.hold}>
                  <input
                    type="checkbox"
                    checked={!!s.held}
                    disabled={busy}
                    data-testid="step-hold"
                    onChange={(e) => {
                      const hold = e.currentTarget.checked;
                      void act(async () => {
                        await api.holdStep(missionId, s.id, hold);
                      });
                    }}
                  />
                  Ask me before this step starts
                </label>
              ) : null}
            </li>
          );
        })}
      </ul>
      <dialog
        ref={dialog}
        className={styles.dialog}
        aria-labelledby={label}
        onClose={() => {
          setPending(null);
          setError("");
        }}
      >
        {error && pending ? (
          <p
            className={styles.alert}
            role="alert"
            data-testid="steps-dialog-error"
          >
            <b>REFUSED</b>
            {error}
          </p>
        ) : null}
        {pending?.kind === "stop" ? (
          <>
            <div className={styles.dhead}>
              <h2 id={label} className={styles.dtag}>
                Stop {pending.step.id}?
              </h2>
            </div>
            <p className={styles.dbody}>
              BattleLab asks its session
              {pending.run.session_key
                ? ` ${shortKey(pending.run.session_key)}`
                : ""}{" "}
              to stop, with everything it started in the background.
            </p>
            <ul className={styles.dlist}>
              <li>
                Only when the stop is proved does the step become <b>stopped</b>{" "}
                and give back its slot and its folder. If it can't be proved, it
                shows <b>unknown</b> and keeps both.
              </li>
              <li>What the judge already accepted stays accepted.</li>
              <li>
                Start it again later as a new episode, in a new session. Nothing
                restarts on its own.
              </li>
            </ul>
            <div className={styles.dactions}>
              <button
                type="button"
                className={`${action.ghost} ${styles.btn}`}
                onClick={() => setPending(null)}
              >
                Keep running
              </button>
              <button
                type="button"
                className={`${action.dangerFilled} ${styles.btn}`}
                disabled={busy}
                onClick={() => {
                  const p = pending;
                  void act(async () => {
                    const out = await api.stopStep(
                      missionId,
                      p.step.id,
                      p.run.episode,
                    );
                    return out.run.state === "stopped"
                      ? `${p.step.id} was stopped.`
                      : `${p.step.id}: ${out.run.detail ?? out.run.state}`;
                  });
                }}
              >
                Stop step
              </button>
            </div>
          </>
        ) : null}
        {pending?.kind === "redirect" ? (
          <>
            <div className={styles.dhead}>
              <h2 id={label} className={styles.dtag}>
                Redirect {pending.step.id}
              </h2>
            </div>
            <p className={styles.dbody}>
              One message into the step's session. It reaches the agent between
              turns, like any message you send it; nothing in the plan changes.
            </p>
            <label className={styles.sid} htmlFor={`${label}-text`}>
              Message
            </label>
            <textarea
              id={`${label}-text`}
              className={styles.text}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <div className={styles.dactions}>
              <button
                type="button"
                className={`${action.ghost} ${styles.btn}`}
                onClick={() => setPending(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className={`${action.primary} ${styles.btn}`}
                disabled={busy || !text.trim() || !pending.run.session_key}
                onClick={() => {
                  const body = text.trim();
                  // The same text retries under the same id (it observes a send that may have
                  // landed); an edited text is a new message with a new id.
                  const p =
                    pending.tried !== undefined && pending.tried !== body
                      ? { ...pending, op: crypto.randomUUID(), tried: body }
                      : { ...pending, tried: body };
                  setPending(p);
                  void act(async () => {
                    await api.structuredSubmit(
                      p.run.session_key as string,
                      p.op,
                      body,
                      undefined,
                    );
                    return `Sent to ${p.step.id}.`;
                  });
                }}
              >
                Send to {pending.step.id}
              </button>
            </div>
          </>
        ) : null}
        {pending?.kind === "pause" ? (
          <>
            <div className={styles.dhead}>
              <h2 id={label} className={styles.dtag}>
                Pause new steps
              </h2>
            </div>
            <p className={styles.dbody}>
              No new step starts, and no automatic turn is sent, until you
              continue. Steps already running:
            </p>
            <fieldset className={styles.choices}>
              <legend className={styles.sid}>Steps already running</legend>
              <label className={styles.choice}>
                <input
                  type="radio"
                  name={`${label}-pause`}
                  checked={!interrupt}
                  onChange={() => setInterrupt(false)}
                />
                <span>Let them finish their current turn</span>
              </label>
              <label className={styles.choice}>
                <input
                  type="radio"
                  name={`${label}-pause`}
                  checked={interrupt}
                  onChange={() => setInterrupt(true)}
                />
                <span>
                  Interrupt them now. They stay running and take your messages.
                </span>
              </label>
            </fieldset>
            <div className={styles.dactions}>
              <button
                type="button"
                className={`${action.ghost} ${styles.btn}`}
                onClick={() => setPending(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className={`${action.primary} ${styles.btn}`}
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    const out = await api.pauseSteps(missionId, interrupt);
                    const parts = ["Paused."];
                    if (out.interrupted.length)
                      parts.push(`Interrupted ${out.interrupted.join(", ")}.`);
                    if (out.not_interrupted.length)
                      parts.push(
                        `Not interrupted (still being started, or the interrupt failed): ${out.not_interrupted.join(", ")}. Stop them, or interrupt them from their sessions.`,
                      );
                    return parts.join(" ");
                  })
                }
              >
                Pause
              </button>
            </div>
          </>
        ) : null}
        {pending?.kind === "stopall" ? (
          <>
            <div className={styles.dhead}>
              <h2 id={label} className={styles.dtag}>
                Stop the mission's steps?
              </h2>
            </div>
            <ul className={styles.dlist}>
              <li>
                The mission is paused: no step that has not started will start.
              </li>
              <li>
                Every running step is asked to stop. Each reads <b>stopped</b>{" "}
                only once its stop is proved, and <b>unknown</b> otherwise.
              </li>
              <li>
                The mission stays open for you to review and close; its sessions
                stay readable until you archive them.
              </li>
            </ul>
            <div className={styles.dactions}>
              <button
                type="button"
                className={`${action.ghost} ${styles.btn}`}
                onClick={() => setPending(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className={`${action.dangerFilled} ${styles.btn}`}
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    const out = await api.stopAllSteps(missionId);
                    const stopped = out.stopped.filter(
                      (r) => r.state === "stopped",
                    ).length;
                    // Only `unknown` is an unproved stop; a step that finished on its own
                    // meanwhile is not.
                    const left = out.stopped.filter(
                      (r) => r.state === "unknown",
                    ).length;
                    const waiting = out.pending.length;
                    // Anything unproved or still settling is an UNFINISHED stop, said so; the
                    // recorded stop finishes it on the server (Hermes on #2007).
                    if (!left && !waiting)
                      return `Paused, and ${stopped} running step(s) stopped.`;
                    const parts = [
                      `The stop is not finished: ${stopped} step(s) stopped.`,
                    ];
                    if (left)
                      parts.push(
                        `${left} could not be proved stopped; stop them again.`,
                      );
                    if (waiting)
                      parts.push(
                        `${waiting} still being started (${out.pending.map((r) => r.step_id).join(", ")}): each is stopped as soon as its launch settles.`,
                      );
                    return parts.join(" ");
                  })
                }
              >
                Stop steps
              </button>
            </div>
          </>
        ) : null}
      </dialog>
    </section>
  );
}

function capital(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}
