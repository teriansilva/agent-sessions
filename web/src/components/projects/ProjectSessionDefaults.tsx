import { useId } from "react";
import { useConfig } from "../../app/config";
import { engineColor, engineInfo, engineLabel, offeredModels, resolveDefault, useEngineRoster } from "../../app/engineRoster";
import { useStructuredModels } from "../../app/useStructuredModels";
import { levelsOf } from "../../lib/sessionDefaults";
import { cleanLaunchDefaults, describeLaunchDefaults } from "../../lib/launchDefaults";
import type { LaunchDefaults } from "../../types/api";
import styles from "./ProjectSessionDefaults.module.css";

/** THE project session-defaults editor (#1508), shared by the New project wizard and Settings →
 *  Projects. Agent and permission prompts can each stay on the global default. A model and its
 *  effort belong to ONE agent, so they are only offered once the project names its own agent —
 *  a model never floats free of it when the global default agent changes. An API client's models
 *  come from #1503's cached store (painted at once, revalidated behind); a terminal agent's from
 *  the roster; an agent that takes no model at launch says so. Native radios throughout. */
export function ProjectSessionDefaults({
  value,
  onChange,
  disabled = false,
}: {
  value: LaunchDefaults;
  onChange: (next: LaunchDefaults) => void;
  disabled?: boolean;
}) {
  const id = useId();
  const config = useConfig();
  const roster = useEngineRoster();
  const engines = config?.new_session_engines ?? [];
  const runtimeOf = (e: string) => engineInfo(e)?.runtime;
  const apiEngines = engines.filter((e) => runtimeOf(e) === "api" || runtimeOf(e) === "chat");
  const consoleEngines = engines.filter((e) => !apiEngines.includes(e));
  const globalEngine = resolveDefault(
    roster.engines.filter((e) => engines.includes(e.id)),
    config?.agent_defaults?.default_engine,
    "new",
  ).engine;
  const globalBypass = config?.agent_defaults?.bypass ?? true;

  const engine = value.engine ?? "";
  const isApi = !!engine && runtimeOf(engine) === "api";
  const apiModels = useStructuredModels(isApi ? engine : null);
  const apiList = isApi ? apiModels.list : null;
  const consoleModels = !isApi && engine ? offeredModels(engine) : [];
  const takesModel = isApi || (!!engine && engineInfo(engine)?.model_select?.supported === true);
  const models: { id: string; text: string }[] = isApi
    ? (apiList?.status === "ok" ? apiList.models : []).map((m) => ({
        id: m.id,
        text: m.label && m.label !== m.id ? `${m.label} (${m.id})` : m.id,
      }))
    : consoleModels.map((m) => ({
        id: m.id,
        text: m.aliases.length ? `${m.aliases[0]} · ${m.id}` : m.id,
      }));
  const model = value.model ?? "";
  const levels = isApi && model ? levelsOf(apiList, model) : [];
  const canBypass = !isApi || engineInfo(engine)?.api?.can_bypass === true;

  const set = (patch: Partial<LaunchDefaults>) => onChange(cleanLaunchDefaults({ ...value, ...patch }));
  const tile = (e: string, label: string, sub?: string) => (
    <label key={e || "global"} className={styles.tile}>
      <input
        type="radio"
        name={`${id}-agent`}
        checked={engine === e}
        disabled={disabled}
        onChange={() => set({ engine: e || undefined, model: undefined, effort: undefined })}
      />
      <span className={styles.tileName}>
        {e && (
          <span className={styles.swatch} style={{ background: engineColor(e) }} aria-hidden="true" />
        )}
        {label}
      </span>
      {sub && <span className={styles.tileSub}>{sub}</span>}
    </label>
  );

  return (
    <div className={styles.root} data-testid="project-session-defaults">
      <div role="radiogroup" aria-label="Agent for new sessions" className={styles.group}>
        <div className={styles.tiles}>
          {tile("", "Global default", `now ${globalEngine ? engineLabel(globalEngine) : "none"}`)}
        </div>
        {consoleEngines.length > 0 && (
          <>
            <span className={styles.groupHead}>Terminal</span>
            <div className={styles.tiles}>{consoleEngines.map((e) => tile(e, engineLabel(e)))}</div>
          </>
        )}
        {apiEngines.length > 0 && (
          <>
            <span className={styles.groupHead}>API — structured, no terminal</span>
            <div className={styles.tiles}>
              {apiEngines.map((e) => {
                const source = engineInfo(e)?.api?.source;
                return tile(
                  e,
                  engineLabel(e),
                  runtimeOf(e) === "chat" ? "chat · no tools" : source ? `via ${engineLabel(source)} CLI` : undefined,
                );
              })}
            </div>
          </>
        )}
        {/* A stored agent this host no longer offers stays named, so saving cannot drop it
            silently; the New session form falls back to the global default for it. */}
        {engine && !engines.includes(engine) && (
          <p className={styles.note} role="status">
            The project’s agent {engineLabel(engine)} isn’t available here — new sessions use the
            global default until it is.
          </p>
        )}
      </div>

      <div className={styles.row2}>
        {!engine ? (
          <p className={styles.note} data-testid="project-defaults-model-needs-agent">
            Model: pick an agent to choose its model. On the global default, the agent’s own default
            model is used.
          </p>
        ) : !takesModel ? (
          <p className={styles.note} data-testid="project-defaults-model-elsewhere">
            Model: set in {engineLabel(engine)}’s own configuration.
          </p>
        ) : (
          <label className={styles.field}>
            <span>Model</span>
            <select
              aria-label="Model for new sessions"
              value={model}
              disabled={disabled}
              onChange={(e) => set({ model: e.target.value || undefined, effort: undefined })}
            >
              <option value="">{engineLabel(engine)}’s default</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.text}
                </option>
              ))}
              {model && !models.some((m) => m.id === model) && (
                <option value={model}>
                  {model} {isApi && apiList === null ? "(checking…)" : "(no longer offered)"}
                </option>
              )}
            </select>
            {isApi && apiList === null && (
              <span className={styles.hint}>Asking {engineLabel(engine)} which models it offers…</span>
            )}
            {isApi && apiList?.status === "unavailable" && (
              <span className={styles.hint}>
                Models unavailable — {apiList.reason ?? "the agent listed no models"}.
              </span>
            )}
          </label>
        )}
        {isApi && model && levels.length > 0 && (
          <label className={styles.field}>
            <span>Reasoning effort</span>
            <select
              aria-label="Reasoning effort for new sessions"
              value={value.effort ?? ""}
              disabled={disabled}
              onChange={(e) => set({ effort: e.target.value || undefined })}
            >
              <option value="">model’s default</option>
              {levels.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <div role="radiogroup" aria-label="Permission prompts for new sessions" className={styles.group}>
        <span className={styles.groupHead}>Permission prompts</span>
        <div className={styles.seg}>
          {(
            [
              [undefined, `Global default · ${globalBypass ? "skip" : "ask"}`],
              [false, "Ask every time"],
              [true, "Skip prompts"],
            ] as const
          ).map(([v, label]) => (
            <label key={String(v)} className={styles.segOpt}>
              <input
                type="radio"
                name={`${id}-bypass`}
                checked={value.bypass === v}
                disabled={disabled}
                onChange={() => set({ bypass: v })}
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
        {engine && !canBypass && (
          <p className={styles.note}>
            {engineLabel(engine)} can’t skip prompts, so its sessions always ask.
          </p>
        )}
      </div>

      <p className={styles.summary} data-testid="project-defaults-summary">
        New sessions start as{" "}
        <b>{describeLaunchDefaults(value, { engine: globalEngine, bypass: globalBypass })}</b>. The
        New session form can still change any of it for one session.
      </p>
    </div>
  );
}
