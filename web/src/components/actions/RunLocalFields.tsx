/** "Run a command" and "Run a script" in the wizard's What step (#2248 P3).
 *
 *  The argument list is edited one element per row. A command's first row is the PROGRAM, always
 *  fixed; every other row is a fixed text or, on a webhook from the list, ONE typed value from the
 *  delivery that becomes exactly that argument. A row marked as an option keeps the row after it
 *  fixed too (an option's value is part of the command). A script is picked from the `#!` files in
 *  the project's folder (`GET /api/actions/scripts`); the server checks the pick again at save and
 *  pins its checksum when you approve. Nothing here runs anything, and nothing decides what is
 *  allowed: the server's validation and consent lines are the authority. */
import { useEffect, useState } from "react";

import { api } from "../../lib/api";
import {
  MAPPED_TYPE_WORDS,
  RUN_ARGS_MAX,
  RUN_ENV_MAX,
  RUN_OUTPUTS_MAX,
  RUN_OUTPUT_TYPE_WORDS,
  RUN_TIMEOUT_MAX,
  errorWords,
  runArgIsOption,
  type EditorForm,
  type MappingDraft,
  type RunArgDraft,
  type RunEnvDraft,
  type RunOutputDraft,
} from "../../lib/automations";
import type { ActionScripts } from "../../types/actions";
import type { MappedType, RunOutputType } from "../../types/automations";
import type { ShapeField } from "../../types/webhooks";
import styles from "../automations/automations.module.css";
import { Seg } from "../automations/wizard/bits";
import w from "../automations/wizard/wizard.module.css";

type Update = (fn: (f: EditorForm) => EditorForm) => void;

export function RunLocalFields({
  form,
  update,
  f,
  listHook,
  shape,
  projects,
}: {
  form: EditorForm;
  update: Update;
  f: (name: string) => string;
  listHook: boolean;
  shape: ShapeField[];
  projects: { id: string; name: string; default_folder?: string | null }[];
}) {
  const command = form.actionKind === "run_command";
  const project = projects.find((p) => p.id === form.projectId);
  const setArg = (i: number, patch: Partial<RunArgDraft>) =>
    update((x) => ({ ...x, runArgs: x.runArgs.map((a, j) => (j === i ? { ...a, ...patch } : a)) }));
  const setEnv = (i: number, patch: Partial<RunEnvDraft>) =>
    update((x) => ({ ...x, runEnv: x.runEnv.map((e, j) => (j === i ? { ...e, ...patch } : e)) }));
  const setOut = (i: number, patch: Partial<RunOutputDraft>) =>
    update((x) => ({ ...x, runOutputs: x.runOutputs.map((o, j) => (j === i ? { ...o, ...patch } : o)) }));
  const shapeList = f("run-shape");
  return (
    <div className={styles.fields} data-testid="run-local-fields">
      <datalist id={shapeList}>
        {shape
          .filter((s) => !s.path.includes("[]") && s.type !== "list" && s.type !== "object")
          .map((s) => (
            <option key={s.path} value={s.path}>
              {s.type}
            </option>
          ))}
      </datalist>
      <div className={styles.field}>
        <label className={styles.label} htmlFor={f("run-project")}>
          Runs in project
        </label>
        <select
          id={f("run-project")}
          className={styles.control}
          value={form.projectId}
          onChange={(e) => update((x) => ({ ...x, projectId: e.target.value, runScript: "" }))}
          data-testid="run-project"
        >
          <option value="">Choose a project…</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
              {p.default_folder ? ` · ${p.default_folder}` : ""}
            </option>
          ))}
          {form.projectId && !project && <option value={form.projectId}>{form.projectId}</option>}
        </select>
        <p className={styles.hint}>
          It starts in {project?.default_folder ? <code>{project.default_folder}</code> : "the project’s folder"}.
        </p>
      </div>
      {!command && <ScriptPicker form={form} update={update} f={f} />}

      <div className={`${styles.field} ${styles.full}`}>
        <span className={styles.label}>{command ? "Program and arguments" : "Extra arguments"}</span>
        <div data-testid="run-args">
          {command && (
            <div className={w.valueRow} data-testid="run-arg-program">
              <div className={w.valueHead}>
                <span className={styles.mono} aria-hidden="true">
                  0
                </span>
                <input
                  className={`${styles.control} ${styles.mono}`}
                  value={form.runProgram}
                  placeholder="make"
                  aria-label="The program (argument 0, always fixed)"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  maxLength={1024}
                  onChange={(e) => update((x) => ({ ...x, runProgram: e.target.value }))}
                  data-testid="run-program"
                />
                <span className={styles.hint}>the program · always fixed</span>
              </div>
            </div>
          )}
          {form.runArgs.map((a, i) => {
            const n = i + 1;
            const lockedByOption = runArgIsOption(form.runArgs[i - 1], listHook);
            const fromDelivery = !!a.map && listHook;
            return (
              <div key={i} className={w.valueRow} data-testid="run-arg">
                <div className={w.valueHead}>
                  <span className={styles.mono} aria-hidden="true">
                    {n}
                  </span>
                  {listHook && !lockedByOption && !runArgIsOption(a, listHook) && (
                    <Seg<"fixed" | "delivery">
                      label={`Where argument ${n} comes from`}
                      value={a.map ? "delivery" : "fixed"}
                      onChange={(v) =>
                        setArg(i, { map: v === "delivery" ? { path: "", type: "text", choices: [] } : null })
                      }
                      testId="run-arg-source"
                      options={[
                        { value: "fixed", label: "Fixed" },
                        { value: "delivery", label: "From the delivery" },
                      ]}
                    />
                  )}
                  <button
                    type="button"
                    className={w.remove}
                    aria-label={`Remove argument ${n}`}
                    onClick={() => update((x) => ({ ...x, runArgs: x.runArgs.filter((_, j) => j !== i) }))}
                    data-testid="run-arg-remove"
                  >
                    ×
                  </button>
                </div>
                {fromDelivery ? (
                  <MapRow
                    map={a.map!}
                    list={shapeList}
                    what={`Argument ${n}`}
                    onChange={(map) => setArg(i, { map })}
                    testId="run-arg"
                  />
                ) : (
                  <div className={w.mapFields}>
                    <input
                      className={`${styles.control} ${styles.mono}`}
                      value={a.value}
                      aria-label={`Argument ${n}: value`}
                      autoCapitalize="none"
                      autoCorrect="off"
                      spellCheck={false}
                      maxLength={1024}
                      onChange={(e) => setArg(i, { value: e.target.value })}
                      data-testid="run-arg-value"
                    />
                    <label className={styles.check}>
                      <input
                        type="checkbox"
                        checked={a.option}
                        onChange={(e) => {
                          const on = e.target.checked;
                          update((x) => ({
                            ...x,
                            runArgs: x.runArgs.map((r, j) =>
                              j === i ? { ...r, option: on } : on && j === i + 1 ? { ...r, map: null } : r,
                            ),
                          }));
                        }}
                        data-testid="run-arg-option"
                      />
                      Option (the next row is its value, fixed)
                    </label>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <div className={w.actions}>
          <button
            type="button"
            className={styles.ghost}
            disabled={form.runArgs.length >= RUN_ARGS_MAX}
            onClick={() =>
              update((x) => ({ ...x, runArgs: [...x.runArgs, { value: "", option: false, map: null }] }))
            }
            data-testid="run-arg-add"
          >
            Add an argument
          </button>
        </div>
        <p className={styles.hint}>
          Each row is exactly one argument, passed as it is: no shell reads it, so quotes, spaces,
          <code>;</code> or <code>$(…)</code> are plain text.
          {listHook &&
            " A value from the delivery is one inert argument, never part of another, and never right after a row starting with -. What the program does with it is the program’s: BattleLab refuses the positions known to take code, such as anything before an interpreter’s script (python3 <value>, awk <value>)."}
        </p>
      </div>

      <div className={`${styles.field} ${styles.full}`}>
        <span className={styles.label}>Environment variables</span>
        <div data-testid="run-env">
          {form.runEnv.map((e, i) => (
            <div key={i} className={w.valueRow} data-testid="run-env-row">
              <div className={w.valueHead}>
                <input
                  className={`${styles.control} ${styles.mono}`}
                  value={e.name}
                  placeholder="NAME"
                  aria-label={`Variable ${i + 1}: name`}
                  autoCapitalize="characters"
                  autoCorrect="off"
                  spellCheck={false}
                  maxLength={64}
                  onChange={(ev) => setEnv(i, { name: ev.target.value.toUpperCase() })}
                  data-testid="run-env-name"
                />
                {listHook && (
                  <Seg<"fixed" | "delivery">
                    label={`Where variable ${i + 1} comes from`}
                    value={e.map ? "delivery" : "fixed"}
                    onChange={(v) =>
                      setEnv(i, { map: v === "delivery" ? { path: "", type: "text", choices: [] } : null })
                    }
                    testId="run-env-source"
                    options={[
                      { value: "fixed", label: "Fixed" },
                      { value: "delivery", label: "From the delivery" },
                    ]}
                  />
                )}
                <button
                  type="button"
                  className={w.remove}
                  aria-label={`Remove variable ${i + 1}`}
                  onClick={() => update((x) => ({ ...x, runEnv: x.runEnv.filter((_, j) => j !== i) }))}
                  data-testid="run-env-remove"
                >
                  ×
                </button>
              </div>
              {e.map && listHook ? (
                <MapRow
                  map={e.map}
                  list={shapeList}
                  what={`Variable ${e.name || i + 1}`}
                  onChange={(map) => setEnv(i, { map })}
                  testId="run-env"
                />
              ) : (
                <input
                  className={`${styles.control} ${styles.mono}`}
                  value={e.value}
                  aria-label={`Variable ${i + 1}: value`}
                  maxLength={1024}
                  onChange={(ev) => setEnv(i, { value: ev.target.value })}
                  data-testid="run-env-value"
                />
              )}
            </div>
          ))}
        </div>
        <div className={w.actions}>
          <button
            type="button"
            className={styles.ghost}
            disabled={form.runEnv.length >= RUN_ENV_MAX}
            onClick={() => update((x) => ({ ...x, runEnv: [...x.runEnv, { name: "", value: "", map: null }] }))}
            data-testid="run-env-add"
          >
            Add a variable
          </button>
        </div>
        <p className={styles.hint}>
          It starts with only PATH, HOME, LANG, TERM=dumb and these. A value that looks like a
          credential is refused: a secret never goes into a command as a value.
        </p>
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={f("run-timeout")}>
          Time limit (minutes)
        </label>
        <input
          id={f("run-timeout")}
          className={styles.control}
          type="number"
          inputMode="numeric"
          min={1}
          max={RUN_TIMEOUT_MAX}
          value={Number.isFinite(form.runTimeout) ? form.runTimeout : ""}
          onChange={(e) => update((x) => ({ ...x, runTimeout: Math.trunc(Number(e.target.value)) }))}
          data-testid="run-timeout"
        />
        <p className={styles.hint}>
          At most {RUN_TIMEOUT_MAX}. Then everything it started is stopped. It keeps 64 KB of what it
          prints.
        </p>
      </div>

      <div className={`${styles.field} ${styles.full}`}>
        <span className={styles.label}>Output fields</span>
        <div data-testid="run-outputs">
          {form.runOutputs.map((o, i) => (
            <div key={i} className={w.mapFields} data-testid="run-output">
              <input
                className={`${styles.control} ${styles.mono}`}
                value={o.name}
                placeholder="build_id"
                aria-label={`Output field ${i + 1}: name`}
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                maxLength={32}
                onChange={(e) => setOut(i, { name: e.target.value })}
                data-testid="run-output-name"
              />
              <select
                className={styles.control}
                value={o.type}
                aria-label={`Output field ${i + 1}: type`}
                onChange={(e) => setOut(i, { type: e.target.value as RunOutputType })}
                data-testid="run-output-type"
              >
                {(Object.keys(RUN_OUTPUT_TYPE_WORDS) as RunOutputType[]).map((t) => (
                  <option key={t} value={t}>
                    {RUN_OUTPUT_TYPE_WORDS[t]}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className={w.remove}
                aria-label={`Remove output field ${i + 1}`}
                onClick={() => update((x) => ({ ...x, runOutputs: x.runOutputs.filter((_, j) => j !== i) }))}
                data-testid="run-output-remove"
              >
                ×
              </button>
            </div>
          ))}
        </div>
        <div className={w.actions}>
          <button
            type="button"
            className={styles.ghost}
            disabled={form.runOutputs.length >= RUN_OUTPUTS_MAX}
            onClick={() =>
              update((x) => ({ ...x, runOutputs: [...x.runOutputs, { name: "", type: "text" }] }))
            }
            data-testid="run-output-add"
          >
            Add an output field
          </button>
        </div>
        <p className={styles.hint}>
          exit_code is always reported. These are read from the last line it prints, if that line is
          one JSON object.
        </p>
      </div>

      <ul className={`${styles.hint} ${styles.full}`} data-testid="run-local-safety">
        <li>
          <strong>No shell.</strong> {command ? "The program" : "The file"} is started directly with
          exactly these arguments. For pipes, redirects or &amp;&amp;,{" "}
          {command ? "write a script and use Run a script." : "put them inside the script."}
        </li>
        <li>
          <strong>Pinned.</strong> {command ? "The program’s" : "The script’s"} sha256 is recorded when
          you approve. If it changes, the automation pauses and asks you again.
        </li>
        <li>
          <strong>Contained.</strong> It runs in its own process scope with a clean environment, a
          time limit and an output cap. Without a scope it is refused.
        </li>
        <li data-testid="run-local-reach">
          <strong>What it can reach.</strong> It runs as your user: it can read and change what you
          can, and reach the network. The project folder is only where it starts; neither the folder
          nor the scope is a sandbox.
        </li>
      </ul>
    </div>
  );
}

function MapRow({
  map,
  list,
  what,
  onChange,
  testId,
}: {
  map: MappingDraft;
  list: string;
  what: string;
  onChange: (m: MappingDraft) => void;
  testId: string;
}) {
  return (
    <div className={w.mapFields}>
      <input
        className={`${styles.control} ${styles.mono}`}
        list={list}
        value={map.path}
        maxLength={160}
        placeholder="pull_request.number"
        aria-label={`${what}: field of the delivery`}
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        onChange={(e) => onChange({ ...map, path: e.target.value.trim() })}
        data-testid={`${testId}-path`}
      />
      <select
        className={styles.control}
        value={map.type}
        aria-label={`${what}: read as`}
        onChange={(e) => onChange({ ...map, type: e.target.value as MappedType })}
        data-testid={`${testId}-type`}
      >
        {(Object.keys(MAPPED_TYPE_WORDS) as MappedType[])
          .filter((t) => t !== "choice")
          .map((t) => (
            <option key={t} value={t}>
              {MAPPED_TYPE_WORDS[t]}
            </option>
          ))}
      </select>
    </div>
  );
}

/** The `#!` files in the project's folder, read when the project is chosen. A pick that is not in
 *  the list (a saved automation's, or a file since removed) stays visible, so nothing is silently
 *  changed. */
function ScriptPicker({ form, update, f }: { form: EditorForm; update: Update; f: (n: string) => string }) {
  const [read, setRead] = useState<{ project: string; scripts: ActionScripts | null; error: string }>({
    project: "",
    scripts: null,
    error: "",
  });
  const projectId = form.projectId;
  useEffect(() => {
    if (!projectId) return;
    let live = true;
    api.actionScripts(projectId).then(
      (got) => live && setRead({ project: projectId, scripts: got, error: "" }),
      (e: unknown) =>
        live && setRead({ project: projectId, scripts: null, error: errorWords(e, "Couldn’t list its scripts.") }),
    );
    return () => {
      live = false;
    };
  }, [projectId]);
  const current = read.project === projectId ? read : { scripts: null, error: "" };
  const listed = current.scripts?.scripts ?? [];
  const kept = form.runScript && !listed.some((s) => s.path === form.runScript);
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={f("run-script")}>
        Script file
      </label>
      <select
        id={f("run-script")}
        className={`${styles.control} ${styles.mono}`}
        value={form.runScript}
        disabled={!projectId}
        onChange={(e) => update((x) => ({ ...x, runScript: e.target.value }))}
        data-testid="run-script"
      >
        <option value="">
          {!projectId
            ? "Choose a project first"
            : current.scripts
              ? listed.length
                ? "Choose a script…"
                : "No script with a #! line here"
              : current.error
                ? "Couldn’t list its scripts"
                : "Reading its scripts…"}
        </option>
        {listed.map((s) => (
          <option key={s.path} value={s.path}>
            {s.path}
          </option>
        ))}
        {kept && <option value={form.runScript}>{form.runScript}</option>}
      </select>
      {current.error ? (
        <p className={styles.hint} role="alert" data-testid="run-script-error">
          {current.error}
        </p>
      ) : (
        <p className={styles.hint}>
          A file in the project’s folder whose first line is <code>#!</code> and an interpreter.
          {current.scripts?.truncated ? " Only the first ones are listed." : ""} Its sha256 is pinned
          when you approve, and each run executes exactly the checked bytes.
        </p>
      )}
    </div>
  );
}
