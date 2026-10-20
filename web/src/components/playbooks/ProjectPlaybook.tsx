/** Your playbooks and the existing deploy contract inside the New project wizard (#1187), laid out
 *  to use the page (#1508): the library is a grid of radio cards, inputs are cards whose source
 *  picker shows what each choice resolves to, and the agent steps are one table. */
import { useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import {
  engineColor,
  engineInfo,
  engineLabel,
  offeredModels,
  useEngineRoster,
} from "../../app/engineRoster";
import { settingsPath } from "../../routes/settingsTabs";
import type { PlaybookCardData } from "../../types/playbooks";
import { ProjectPlaybookRemove } from "./ProjectPlaybookRemove";
import { inputState, sourceLabels, type Globals } from "./playbookInputState";
import { playbookMissing, usePlaybookGlobals } from "./usePlaybookGlobals";
import type { useProjectPlaybook } from "./useProjectPlaybook";
import buttons from "../ui/actionButton.module.css";
import styles from "./ProjectPlaybook.module.css";

export type ProjectPlaybookState = ReturnType<typeof useProjectPlaybook>;

function counts(card: PlaybookCardData): string[] {
  const steps = (card.flows ?? []).reduce(
    (n, f) => n + f.steps.filter((s) => s.actor.kind === "agent").length,
    0,
  );
  const out: string[] = [];
  const n = (count: number, noun: string) => `${count} ${noun}${count === 1 ? "" : "s"}`;
  if (card.ships) {
    out.push(n(card.ships.materials, "file"));
    out.push(n(card.ships.variables, "input"));
  }
  out.push(n(steps, "agent step"));
  return out;
}

export function ProjectPlaybookLibrary({
  book,
}: {
  book: ProjectPlaybookState;
}) {
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("");
  const shown = (book.library?.playbooks ?? []).filter(
    (p) =>
      (!source || p.source === source) &&
      [
        p.id,
        p.name,
        p.summary,
        ...(p.flows ?? []).flatMap((f) => f.steps.map((s) => s.title)),
      ]
        .join(" ")
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  return (
    <div className={styles.root}>
      <p className={styles.lede}>
        Give this project a reviewed set of files, inputs and workflow
        instructions. Choosing one starts no agents and runs no probes.{" "}
        <span data-testid="project-playbook-gallery-hint">
          Playbooks you add from the gallery appear here.
        </span>
      </p>
      {book.defaultNotice && (
        <p role="status" className={styles.warning}>
          {book.defaultNotice}
        </p>
      )}
      {book.libraryError && (
        <div role="alert" className={styles.warning}>
          {book.libraryError}{" "}
          <button
            className={buttons.ghost}
            onClick={() => void book.reloadLibrary()}
          >
            Reload playbooks
          </button>
        </div>
      )}
      <div className={styles.bar}>
        <label>
          <span className="sr-only">Search playbooks</span>
          <input
            type="search"
            aria-label="Search playbooks"
            placeholder="Search playbooks and steps…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <label>
          <span className="sr-only">Source</span>
          <select
            aria-label="Source"
            value={source}
            onChange={(e) => setSource(e.target.value)}
          >
            <option value="">All sources</option>
            <option value="local">Local</option>
            <option value="bundled">Bundled</option>
            <option value="catalog">Catalog</option>
          </select>
        </label>
      </div>
      {/* NATIVE radios drawn as cards, like New session's agent tiles (#1436): arrow keys, Tab
          order and screen-reader semantics are the browser's own. */}
      <div className={styles.library} role="radiogroup" aria-label="Playbook">
        <label className={`${styles.option} ${styles.none}`}>
          <input
            type="radio"
            name="project-playbook"
            checked={!book.id}
            onChange={() => book.select(null)}
          />
          <span className={styles.optionHead}>
            <b>No playbook</b>
            <span className={styles.optionHint}>
              Create the project without files or workflow instructions.
            </span>
          </span>
        </label>
        {shown.map((card) => {
          const on = card.id === book.id;
          return (
            <div
              key={`${card.source}:${card.id}`}
              className={styles.optionCell}
              data-testid={`playbook-card-${card.id}`}
            >
              <label className={`${styles.option} ${card.ok ? "" : styles.invalid}`}>
                <input
                  type="radio"
                  name="project-playbook"
                  checked={on}
                  disabled={!card.ok}
                  onChange={() => book.select(card.id)}
                />
                <span className={styles.optionHead}>
                  <b>{card.name || card.id}</b>
                  <span className={styles.sourceTag}>{card.source}</span>
                  {card.default && <span className={styles.sourceTag}>default</span>}
                </span>
                {card.ok ? (
                  <>
                    {card.summary && (
                      <span className={styles.optionHint}>{card.summary}</span>
                    )}
                    {!!card.flows?.length && (
                      <span className={styles.chips}>
                        {card.flows.flatMap((f) =>
                          f.steps.map((s) => (
                            <span key={`${f.id}:${s.id}`}>{s.title}</span>
                          )),
                        )}
                      </span>
                    )}
                    <span className={styles.counts}>
                      {counts(card).map((c) => (
                        <span key={c}>{c}</span>
                      ))}
                    </span>
                  </>
                ) : (
                  <span className={styles.warning}>
                    Could not be loaded: {card.error}
                  </span>
                )}
              </label>
              {on && book.detail?.source === "local" && !book.loadError && (
                <label className={styles.check}>
                  <input
                    type="checkbox"
                    checked={book.trust}
                    onChange={(e) => book.setTrust(e.target.checked)}
                  />
                  I have reviewed this unsigned local playbook and trust its
                  instructions and proposed files.
                </label>
              )}
            </div>
          );
        })}
      </div>
      {!book.library && !book.libraryError && (
        <p role="status">Loading playbooks…</p>
      )}
      {book.library && !shown.length && (
        <p>No playbook matches. No playbook remains available.</p>
      )}
      {book.loading && <p role="status">Loading selected playbook…</p>}
      {book.loadError && (
        <p role="alert" className={styles.warning}>
          {book.loadError}
        </p>
      )}
    </div>
  );
}

/** One model option as the agent names it: `alias · id` where it gives an alias. */
function modelText(m: { id: string; aliases: string[] }): string {
  return m.aliases.length ? `${m.aliases[0]} · ${m.id}` : m.id;
}

export function ProjectPlaybookInputs({
  book,
  globals,
}: {
  book: ProjectPlaybookState;
  globals: Globals;
}) {
  const roster = useEngineRoster();
  const [everyStep, setEveryStep] = useState("");
  const steps = (book.detail?.flows ?? []).flatMap((f) =>
    f.steps
      .filter((s) => s.actor.kind === "agent")
      .map((s) => ({ ...s, key: `${f.id}:${s.id}`, flow: f.title, flowId: f.id })),
  );
  const available = roster.engines.filter(
    (e) => e.present && e.status !== "retiring" && book.agents.includes(e.id),
  );
  const assignmentOf = (step: (typeof steps)[number]) =>
    step.key in book.assignments
      ? book.assignments[step.key]
      : step.actor.engine
        ? { engine: step.actor.engine, model: step.actor.model || "default" }
        : null;
  const missing = playbookMissing(book, globals);
  const binaries = Object.entries(book.detail?.requires_present?.binaries ?? {});
  return (
    <div className={styles.root}>
      <section className={styles.section} aria-labelledby="pb-inputs-h">
        <div className={styles.sectionHead}>
          <h2 id="pb-inputs-h">Inputs</h2>
          <span className={styles.sectionMeta}>
            {missing.length > 0 && (
              <span className={styles.missingCount} data-testid="pb-missing-count">
                {missing.length} required missing ·{" "}
              </span>
            )}
            {book.variables.length} variable{book.variables.length === 1 ? "" : "s"}
          </span>
        </div>
        <p className={styles.lede}>
          Inputs stay in this wizard until you create the project and bind them.
          Secret values are never shown in the review.
        </p>
        {!book.variables.length && <p>This playbook declares no variable inputs.</p>}
        <div className={styles.vars}>
          {book.variables.map((variable) => {
            const choice = book.choices[variable.name] ?? {
              mode: "inherit" as const,
              value: "",
            };
            const update = (patch: Partial<typeof choice>) =>
              book.setChoices((all) => ({
                ...all,
                [variable.name]: { ...choice, ...patch },
              }));
            const secret = variable.kind === "secret";
            const labels = sourceLabels(variable, globals, book.projectExists);
            const isMissing =
              !!variable.required &&
              inputState(variable, choice, globals, book.projectExists) === "missing";
            const errId = `pb-missing-${variable.name}`;
            return (
              <fieldset
                key={variable.name}
                className={isMissing ? `${styles.var} ${styles.varMissing}` : styles.var}
                disabled={book.busy}
                data-testid={`pb-input-${variable.name}`}
              >
                <legend className="sr-only">
                  {variable.label || variable.name}
                  {variable.required ? " · required" : ""}
                </legend>
                <div className={styles.varHead} aria-hidden="true">
                  <code>{variable.name}</code>
                  <span className={styles.varType}>
                    {variable.required && (
                      <span className={isMissing ? styles.reqMissing : undefined}>
                        required ·{" "}
                      </span>
                    )}
                    {secret ? "secret" : variable.type || "text"}
                  </span>
                </div>
                {variable.label && variable.label !== variable.name && (
                  <p className={styles.varLabel}>{variable.label}</p>
                )}
                {variable.help && <p className={styles.optionHint}>{variable.help}</p>}
                <div
                  className={styles.seg}
                  role="radiogroup"
                  aria-label={`Source for ${variable.name}`}
                >
                  {(
                    [
                      ["value", labels.value],
                      ["global", labels.global],
                      ["inherit", labels.inherit],
                    ] as const
                  ).map(([mode, text]) => (
                    <label key={mode} className={styles.segOpt}>
                      <input
                        type="radio"
                        name={`pb-source-${variable.name}`}
                        checked={choice.mode === mode}
                        onChange={() => update({ mode, value: "" })}
                      />
                      <span>{text}</span>
                    </label>
                  ))}
                </div>
                {choice.mode === "value" &&
                  (!secret && (variable.type === "bool" || variable.type === "enum") ? (
                    <select
                      aria-label={`Value for ${variable.name}`}
                      aria-invalid={isMissing || undefined}
                      aria-describedby={isMissing ? errId : undefined}
                      value={choice.value}
                      onChange={(e) => update({ value: e.target.value })}
                    >
                      <option value="">Choose a value</option>
                      {(variable.type === "bool"
                        ? ["true", "false"]
                        : (variable.choices ?? [])
                      ).map((v) => (
                        <option key={v}>{v}</option>
                      ))}
                    </select>
                  ) : secret ? (
                    <input
                      type="password"
                      autoComplete="new-password"
                      aria-label={`Value for ${variable.name}`}
                      aria-invalid={isMissing || undefined}
                      aria-describedby={isMissing ? errId : undefined}
                      placeholder="Enter a value"
                      value={choice.value}
                      onChange={(e) => update({ value: e.target.value })}
                    />
                  ) : (variable.type ?? "text") === "text" ? (
                    // `text` is multiline on the server (`binding._typed`): an instruction block
                    // keeps its newlines, which an <input> would strip.
                    <textarea
                      aria-label={`Value for ${variable.name}`}
                      aria-invalid={isMissing || undefined}
                      aria-describedby={isMissing ? errId : undefined}
                      placeholder={
                        variable.example != null ? `e.g. ${variable.example}` : "Enter a value"
                      }
                      rows={3}
                      value={choice.value}
                      onChange={(e) => update({ value: e.target.value })}
                    />
                  ) : (
                    <input
                      type="text"
                      aria-label={`Value for ${variable.name}`}
                      aria-invalid={isMissing || undefined}
                      aria-describedby={isMissing ? errId : undefined}
                      placeholder={
                        variable.example != null ? `e.g. ${variable.example}` : "Enter a value"
                      }
                      value={choice.value}
                      onChange={(e) => update({ value: e.target.value })}
                    />
                  ))}
                {choice.mode === "global" && (
                  <p className={styles.optionHint}>
                    The global variable <code>{variable.name}</code>
                    {secret ? " · its value stays hidden." : "."} The server
                    checks its name, kind, value and revision before binding.
                  </p>
                )}
                {choice.mode === "inherit" && !isMissing && (
                  <p className={styles.optionHint}>{labels.inheritHint}</p>
                )}
                {isMissing && (
                  <p id={errId} className={styles.err}>
                    Required —{" "}
                    {choice.mode === "value"
                      ? "enter a value."
                      : choice.mode === "global"
                        ? `no global variable ${variable.name} is saved.`
                        : secret
                          ? "enter a value or use a global."
                          : "this playbook declares no default."}
                  </p>
                )}
              </fieldset>
            );
          })}
        </div>
      </section>

      <section className={styles.section} aria-labelledby="pb-steps-h">
        <div className={styles.sectionHead}>
          <h2 id="pb-steps-h">Agents per step</h2>
          <span className={styles.sectionMeta}>saved configuration · starts nothing</span>
        </div>
        {!steps.length && <p>No agent steps need an assignment.</p>}
        {steps.length > 1 && available.length > 0 && (
          <div className={styles.everyStep}>
            <label>
              <span>Set every step to</span>
              <select
                aria-label="Agent for every step"
                value={everyStep}
                disabled={book.busy}
                onChange={(e) => setEveryStep(e.target.value)}
              >
                <option value="">Choose an agent</option>
                {available.map((e) => (
                  <option key={e.id} value={e.id}>
                    {engineLabel(e.id)}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className={buttons.ghost}
              disabled={!everyStep || book.busy}
              onClick={() =>
                book.setAssignments(
                  Object.fromEntries(
                    steps.map((s) => [s.key, { engine: everyStep, model: "default" }]),
                  ),
                )
              }
            >
              Apply
            </button>
          </div>
        )}
        {steps.length > 0 && (
          <div className={styles.steps}>
            <div className={styles.stepsHead} aria-hidden="true">
              <span>Step</span>
              <span>Agent</span>
              <span>Model</span>
              <span>State</span>
            </div>
            {steps.map((step, i) => {
              const assignment = assignmentOf(step);
              const set = (value: { engine: string; model: string } | null) =>
                book.setAssignments((all) => ({ ...all, [step.key]: value }));
              const known = available.some((e) => e.id === assignment?.engine);
              // An agent the roster does not know keeps its requested model on screen (unresolved),
              // rather than being described as configuring its own (#1508 PR review).
              const info = assignment ? engineInfo(assignment.engine) : undefined;
              const takesModel =
                !!assignment && (info ? info.model_select?.supported === true : true);
              const models = assignment ? offeredModels(assignment.engine) : [];
              // A stored alias (`opus`) shows as the model it names: the options are canonical
              // ids, and a select whose value matches no option silently shows its first one.
              const listedAs =
                !assignment || assignment.model === "default"
                  ? undefined
                  : models.find(
                      (m) => m.id === assignment.model || m.aliases.includes(assignment.model),
                    )?.id;
              const modelListed =
                !assignment || assignment.model === "default" || listedAs !== undefined;
              const state = !assignment
                ? { led: styles.ledIdle, text: "unassigned" }
                : roster.status !== "ready" || !known
                  ? { led: styles.ledDown, text: "agent unavailable" }
                  : takesModel && !modelListed
                    ? { led: styles.ledWarn, text: "model not listed" }
                    : { led: styles.ledUp, text: "ready" };
              const newFlow = i === 0 || steps[i - 1].flowId !== step.flowId;
              return (
                <div key={step.key} className={styles.stepGroup}>
                  {newFlow && <div className={styles.flowRow}>{step.flow}</div>}
                  <fieldset
                    className={styles.stepRow}
                    disabled={book.busy}
                    data-testid={`pb-step-${step.key}`}
                  >
                    <legend className="sr-only">
                      {step.flow} · {step.title}
                    </legend>
                    <span className={styles.stepName}>
                      <span className={styles.stepNo} aria-hidden="true">
                        {String(i + 1).padStart(2, "0")}
                      </span>
                      <b>{step.title}</b>
                    </span>
                    <select
                      aria-label={`Agent for ${step.title}`}
                      value={assignment?.engine || ""}
                      style={
                        assignment
                          ? ({ "--eng": engineColor(assignment.engine) } as CSSProperties)
                          : undefined
                      }
                      className={assignment ? styles.engineSelect : undefined}
                      onChange={(e) =>
                        set(
                          e.target.value
                            ? { engine: e.target.value, model: "default" }
                            : null,
                        )
                      }
                    >
                      <option value="">Unassigned</option>
                      {assignment && !known && (
                        <option value={assignment.engine}>
                          {engineLabel(assignment.engine)} · unavailable
                        </option>
                      )}
                      {available.map((engine) => (
                        <option key={engine.id} value={engine.id}>
                          {engineLabel(engine.id)}
                        </option>
                      ))}
                    </select>
                    {!assignment ? (
                      <span className={styles.optionHint}>—</span>
                    ) : takesModel ? (
                      <select
                        aria-label={`Model for ${step.title}`}
                        value={listedAs ?? assignment.model}
                        onChange={(e) => set({ ...assignment, model: e.target.value })}
                      >
                        <option value="default">Agent default</option>
                        {!modelListed && (
                          <option value={assignment.model}>
                            {assignment.model} · not listed
                          </option>
                        )}
                        {models
                          .filter((m) => m.id !== "default")
                          .map((m) => (
                            <option key={m.id} value={m.id}>
                              {modelText(m)}
                            </option>
                          ))}
                      </select>
                    ) : (
                      <span className={styles.optionHint}>
                        Model is set in {engineLabel(assignment.engine)}’s own config
                      </span>
                    )}
                    <span className={styles.state}>
                      <span className={`${styles.led} ${state.led}`} aria-hidden="true" />
                      {state.text}
                    </span>
                    {assignment && (roster.status !== "ready" || !known) && (
                      <p className={`${styles.warning} ${styles.rowNote}`}>
                        This assignment is unresolved. The review preserves the reason.{" "}
                        <Link to={settingsPath("agents")}>Settings → Agents</Link>
                      </p>
                    )}
                  </fieldset>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {binaries.length > 0 && (
        <section className={styles.section} aria-labelledby="pb-req-h">
          <div className={styles.sectionHead}>
            <h2 id="pb-req-h">Requirements</h2>
          </div>
          <ul className={styles.reqs}>
            {binaries.map(([binary, present]) => (
              <li key={binary} className={styles.req}>
                <span
                  className={`${styles.led} ${present ? styles.ledUp : styles.ledDown}`}
                  aria-hidden="true"
                />
                {binary}
                <span className="sr-only">: {present ? "available" : "missing"}</span>
                {!present && <span className={styles.reqMissing}>missing</span>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}


export function ProjectPlaybookReview({
  book,
}: {
  book: ProjectPlaybookState;
}) {
  const plan = book.reviewed;
  return (
    <div className={styles.root}>
      <h2>Playbook changes</h2>
      <p>
        {book.detail?.name || book.id} · no workflow starts during assignment.
      </p>
      {book.error && (
        <p role="alert" className={styles.warning}>
          {book.error}
        </p>
      )}
      <button
        className={buttons.ghost}
        disabled={book.busy}
        onClick={() => void book.review(true)}
      >
        {book.busy
          ? "Reviewing…"
          : plan
            ? "Refresh review"
            : "Review playbook changes"}
      </button>
      {book.reviewingCreatedTarget && <p role="status">
        This wizard already created the folder. Its identity has been checked again.
        Review its current contents and confirm the new plan before creating the project.
      </p>}
      {plan && (
        <>
          <p>
            Review <code className={styles.digest}>{plan.digest}</code>
          </p>
          {!!plan.conflicts.length && (
            <div role="alert" className={styles.warning}>
              <strong>
                Resolve these conflicts before creating the project.
              </strong>
              <ul>
                {plan.conflicts.map((c) => (
                  <li key={c.path}>
                    {c.path}: {c.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <h3>Resolved inputs</h3>
          <dl>
            {plan.variables.map((v) => (
              <div key={v.name}>
                <dt>{v.name}</dt>
                <dd>
                  {v.kind === "secret"
                    ? v.set
                      ? "Secret set · hidden"
                      : "Secret unset"
                    : v.state === "missing"
                      ? "Unset"
                      : String(v.value)}{" "}
                  · {v.source || "no source"}
                  {v.explicit_global ? " · explicit global reference" : ""}
                </dd>
              </div>
            ))}
          </dl>
          <h3>Files</h3>
          {!plan.changes.length && <p>No material changes.</p>}
          {plan.changes.map((change) => (
            <details key={change.path} className={styles.card}>
              <summary>
                {change.action} · {change.path}
              </summary>
              <pre>
                {change.diff ||
                  JSON.stringify(
                    { before: change.before, after: change.after },
                    null,
                    2,
                  )}
              </pre>
            </details>
          ))}
          <h3>Rendered instructions and materials</h3>
          {plan.materials.map((material) => (
            <details key={material.path} className={styles.card}>
              <summary>
                {material.path} · {material.disposition}
              </summary>
              <pre>
                {material.after.text ??
                  material.after.target ??
                  "Binary file; inspect its proposed change above."}
              </pre>
            </details>
          ))}
          <h3>Resolved targets</h3>
          {!plan.targets.length && (
            <p>No connection or completion probe targets.</p>
          )}
          {plan.targets.map((target) => (
            <div key={target.id} className={styles.card}>
              <strong>{target.id}</strong>
              <pre>{JSON.stringify(target.args, null, 2)}</pre>
              {!!Object.keys(target.pending).length && (
                <p>
                  Still unresolved: {Object.keys(target.pending).join(", ")}.
                  Assignment does not probe this target.
                </p>
              )}
              {target.requires_confirmation && (
                <label className={styles.check}>
                  <input
                    type="checkbox"
                    disabled={book.busy}
                    checked={book.confirmed.includes(target.id)}
                    onChange={(e) =>
                      book.setConfirmed((all) =>
                        e.target.checked
                          ? [...all, target.id]
                          : all.filter((id) => id !== target.id),
                      )
                    }
                  />
                  Confirm this target, derived from defaults:{" "}
                  {target.default_variables.join(", ")}
                </label>
              )}
            </div>
          ))}
          <h3>Assignments</h3>
          <ul>
            {plan.assignments.map((a) => (
              <li key={a.step}>
                {a.step}:{" "}
                {a.assignment
                  ? `${engineLabel(a.assignment.engine)} / ${a.assignment.model}`
                  : `Unassigned — ${a.reason}`}
              </li>
            ))}
          </ul>
          <h3>Capability requests</h3>
          <p>
            {plan.capability_requests.join(", ") || "None"}. Assignment grants
            no additional capabilities.
          </p>
          {!!plan.rituals.length && (
            <p>{plan.rituals.length} rituals declared; none scheduled.</p>
          )}
        </>
      )}
    </div>
  );
}

export function ProjectPlaybookOutcome({
  book,
  project,
}: {
  book: ProjectPlaybookState;
  project: string;
}) {
  const globals = usePlaybookGlobals(book.recovering);
  const applied = book.status?.state === "applied";
  return (
    <section className={styles.root} aria-label="Playbook assignment result">
      <h2>Playbook assignment</h2>
      <p>
        Project: created.{" "}
        {book.busy
          ? `${book.stage === "bind" ? "Binding inputs" : book.stage === "apply" ? "Applying files" : "Verifying"}…`
          : ""}
      </p>
      <p>
        Deployment:{" "}
        {book.status
          ? book.status.state
          : book.stage === "done"
            ? "applied"
            : "status unavailable"}
        .
      </p>
      {book.status?.files?.map((f) => (
        <p key={f.path}>
          <code>{f.path}</code>: {f.state}
        </p>
      ))}
      {book.error && (
        <p role="alert" className={styles.warning}>
          {book.error}
          {applied
            ? " Files and bindings were applied; verification can be retried."
            : " The project is retained. Retry uses its existing ID and the same operation."}
        </p>
      )}
      {book.verification && (
        <>
          <h3>
            Verification: {book.verification.ok ? "passed" : "needs attention"}
          </h3>
          <ul>
            {Object.entries(book.verification.checks).map(([name, check]) => (
              <li key={name}>
                {name}:{" "}
                {check.ok === true
                  ? "passed"
                  : check.ok === false
                    ? "needs attention"
                    : "reported"}
                {check.error ? ` · ${check.error}` : ""}
                {check.drift?.length
                  ? ` · drift: ${check.drift.join(", ")}`
                  : ""}
                {check.missing?.length
                  ? ` · missing: ${check.missing.join(", ")}`
                  : ""}
              </li>
            ))}
          </ul>
        </>
      )}
      {!book.recovering &&
        !book.busy &&
        (book.error || book.verification?.ok === false) && (
          <button
            className={buttons.primary}
            onClick={() => void book.deploy(project)}
          >
            {applied
              ? "Re-verify"
              : book.stage === "bind"
                ? "Retry binding"
                : "Retry apply"}
          </button>
        )}
      {!book.recovering &&
        book.error &&
        book.stage !== "verify" &&
        book.status &&
        book.status.state !== "interrupted" && (
          <button
            className={buttons.ghost}
            disabled={book.busy}
            onClick={() => void book.recoverReview()}
          >
            Review current changes for this project
          </button>
        )}
      {(book.status?.state === "applied" ||
        book.status?.state === "bound" ||
        (book.status?.state === "interrupted" &&
          book.status.operation === "apply")) && (
        <ProjectPlaybookRemove
          project={project}
          disabled={book.busy}
          onBusy={book.setRemoving}
          onRemoved={book.removed}
        />
      )}
      {book.recovering && (
        <>
          <ProjectPlaybookInputs book={book} globals={globals} />
          <ProjectPlaybookReview book={book} />
          <button
            className={buttons.primary}
            disabled={book.busy || !book.canCreate}
            onClick={async () => {
              if (await book.prepare()) await book.deploy(project);
            }}
          >
            Apply reviewed changes to this project
          </button>
        </>
      )}
    </section>
  );
}
