import { useRef, useState } from "react";
import { useConfig, useConfigReload } from "../../app/config";
import { useStructuredModels } from "../../app/useStructuredModels";
import { ApiError, api } from "../../lib/api";
import { levelsOf, resolveSessionDefault } from "../../lib/sessionDefaults";
import { structuredModelsStore } from "../../lib/structuredModelsStore";
import button from "../ui/actionButton.module.css";
import s from "./SessionDefaults.module.css";

/** "New sessions" on an API client's agent page (#1407): the model and reasoning level a new
 *  session of this client starts with. They only PRESELECT the new-session screen; each session
 *  can still choose for itself, and a running session keeps what it started with.
 *
 *  Both choices come from the client's OWN list (`/api/structured/clients/{id}/models`), the same
 *  list the server validates the save and every create against. A level belongs to a model, so
 *  none can be saved beside the client's default. The list is the New session form's cached one
 *  (#1503): painted at once, revalidated behind it. */
export function SessionDefaults({
  engine,
  label,
  source,
}: {
  engine: string;
  label: string;
  /** The console agent this client drives — the owner of the model list. */
  source: string;
}) {
  const config = useConfig();
  const reloadConfig = useConfigReload();
  const saved = config?.agent_defaults?.session_defaults?.[engine] ?? null;
  const { list } = useStructuredModels(engine);

  const resolved = resolveSessionDefault(saved, list);
  // The form: null = untouched (shows the resolved default). A draft and a status belong to the
  // client they were made for (#1412 review): on another client's page they read as absent, so
  // an unsaved choice can never be submitted — or a late save announced — for the wrong client.
  const [draft, setForm] = useState<{ engine: string; model: string; effort: string } | null>(null);
  const form = draft?.engine === engine ? draft : null;
  const model = form?.model ?? resolved.model ?? "default";
  const levels = levelsOf(list, model);
  const effort =
    form?.effort ?? (model === resolved.model ? resolved.effort : null) ?? "default";
  const [busy, setBusy] = useState(false);
  const [shown, setShown] = useState<{ engine: string; ok: boolean; text: string } | null>(null);
  const status = shown?.engine === engine ? shown : null;
  const setStatus = (v: { ok: boolean; text: string } | null) =>
    setShown(v && { engine: savingFor.current, ...v });
  // The client a save in flight was made for; its result is filed under it, never the page's
  // current one.
  const savingFor = useRef(engine);

  const listOk = list?.status === "ok";
  const dirty =
    form !== null &&
    (model !== (saved?.model ?? "default") ||
      (model !== "default" && effort !== (saved?.effort ?? "default")));

  const save = async (clear = false) => {
    if (busy) return;
    savingFor.current = engine;
    setBusy(true);
    setStatus(null);
    try {
      await api.setAgentDefaults({
        session_defaults: {
          [engine]:
            clear || model === "default"
              ? null
              : { model, effort: effort === "default" ? null : effort },
        },
      });
      // The new-session screen reads these through the config: "Saved" is announced only once
      // the app reads the new defaults (#1407 PR review), one retry for a superseded/failed read.
      const ok = (await reloadConfig()) || (await reloadConfig());
      setForm(null);
      setStatus(
        ok
          ? {
              ok: true,
              text: clear
                ? `Cleared — new ${label} sessions start with ${source}’s default.`
                : `Saved — new ${label} sessions start with these.`,
            }
          : {
              ok: false,
              text: "Saved, but this page couldn’t reload your settings yet — reload before starting a session.",
            },
      );
    } catch (e) {
      setStatus({
        ok: false,
        text: e instanceof ApiError && e.message ? e.message : "Couldn’t save the defaults.",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={s.root} data-testid="session-defaults">
      <p className={s.lead}>
        What a new {label} session starts with. You can still change it for one session on the
        new-session screen; running sessions keep what they started with.
      </p>
      {resolved.modelStale && (
        <p className={s.warn} role="status" data-testid="session-defaults-stale">
          Saved model <code>{resolved.modelStale}</code> is no longer offered by {source}. New
          sessions use {source}’s default until you pick another.
        </p>
      )}
      {resolved.effortStale && (
        <p className={s.warn} role="status" data-testid="session-defaults-stale">
          Saved reasoning effort <b>{resolved.effortStale}</b> is no longer offered for{" "}
          <code>{model}</code>. New sessions use the model’s default level until you pick another.
        </p>
      )}
      {list && !listOk && (
        <div className={s.error} role="alert" data-testid="session-defaults-unavailable">
          <span>
            Couldn’t ask {source} for its models — {list.reason ?? "no answer"}. Only “{source}’s
            default” can be saved.
          </span>
          <button
            type="button"
            className={button.ghost}
            onClick={() => void structuredModelsStore.revalidate(engine, { force: true })}
          >
            Retry
          </button>
        </div>
      )}
      <label className={s.field}>
        <span>Default model</span>
        <select
          aria-label="Default model"
          data-testid="session-defaults-model"
          value={model}
          disabled={busy || list === null}
          onChange={(e) => {
            const next = e.target.value;
            // A level belongs to a model: keep it only if the new model offers it.
            const keep = levelsOf(list, next).includes(effort) ? effort : "default";
            setForm({ engine, model: next, effort: keep });
            setStatus(null);
          }}
        >
          <option value="default">{source}’s default</option>
          {(list?.models ?? []).map((m) => (
            <option key={m.id} value={m.id}>
              {m.label && m.label !== m.id ? `${m.label} (${m.id})` : m.id}
            </option>
          ))}
          {/* An unsaved choice the list has since dropped (a background revalidation landed
              under the draft) stays visible, so the select shows what Save would send — and the
              server would refuse — never a different option (#1503 PR review). */}
          {model !== "default" && !(list?.models ?? []).some((m) => m.id === model) && (
            <option value={model}>
              {model} {list === null ? "(checking…)" : "(not offered now)"}
            </option>
          )}
        </select>
        <span className={s.hint}>
          {list === null
            ? `Asking ${source} which models it offers…`
            : listOk
              ? `From ${source}’s own model list · ${list.models.length} models`
              : ""}
        </span>
      </label>
      {listOk && (
        <label className={s.field}>
          <span>Reasoning effort</span>
          <select
            aria-label="Default reasoning effort"
            data-testid="session-defaults-effort"
            value={effort}
            disabled={busy || model === "default" || (levels.length === 0 && effort === "default")}
            onChange={(e) => {
              setForm({ engine, model, effort: e.target.value });
              setStatus(null);
            }}
          >
            <option value="default">model’s default</option>
            {levels.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
            {effort !== "default" && !levels.includes(effort) && (
              <option value={effort}>{effort} (not offered now)</option>
            )}
          </select>
          <span className={s.hint}>
            {model === "default"
              ? "Pick a model first: levels belong to a model."
              : levels.length
                ? `Offered: ${levels.join(" · ")}`
                : "This model offers no levels."}
          </span>
        </label>
      )}
      <div className={s.actions}>
        <button
          type="button"
          className={button.primary}
          disabled={busy || !dirty}
          onClick={() => void save()}
        >
          {busy ? "Saving…" : "Save defaults"}
        </button>
        {/* Clearing needs no model list (the server admits it without one), so it stays available
            while the list is loading or unavailable — the select alone could not express it then. */}
        {saved && (
          <button
            type="button"
            className={button.ghost}
            disabled={busy}
            onClick={() => void save(true)}
            data-testid="session-defaults-clear"
          >
            Clear default
          </button>
        )}
        {status && (
          <span
            className={status.ok ? s.ok : s.bad}
            role={status.ok ? "status" : "alert"}
            data-testid="session-defaults-status"
          >
            {status.text}
          </span>
        )}
      </div>
    </div>
  );
}
