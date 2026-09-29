import { useEffect, useState } from "react";
import { useConfigRefresh } from "../../app/config";
import { reloadRoster } from "../../app/reloadRoster";
import { ApiError, api } from "../../lib/api";
import type { AgentEndpoint, AgentEndpointPatch, AgentTools } from "../../types/api";
import styles from "./AgentEndpointCard.module.css";

type Status = { kind: "ok" | "err"; text: string } | null;

/** An API agent's endpoint (#1209): where BattleLab sends its conversations. The key is written,
 *  never read back — the server only says whether one is stored. TEST checks the draft and saves
 *  nothing; SAVE stores it and makes the agent available at once. */
export function AgentEndpointCard({ engine }: { engine: string }) {
  const refreshConfig = useConfigRefresh();
  const [stored, setStored] = useState<AgentEndpoint | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const [model, setModel] = useState("");
  const [context, setContext] = useState("");
  const [output, setOutput] = useState("");
  const [tools, setTools] = useState<AgentTools>("none");
  const [models, setModels] = useState<string[]>([]);
  const [status, setStatus] = useState<Status>(null);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);

  const adopt = (ep: AgentEndpoint) => {
    setStored(ep);
    setUrl(ep.base_url);
    setModel(ep.model);
    setContext(String(ep.context_window));
    setOutput(String(ep.max_output_tokens));
    setTools(ep.tools);
    setKey("");
  };

  useEffect(() => {
    let alive = true;
    api
      .agentEndpoint(engine)
      .then((ep) => {
        if (!alive) return;
        setStored(ep);
        setUrl(ep.base_url);
        setModel(ep.model);
        setContext(String(ep.context_window));
        setOutput(String(ep.max_output_tokens));
        setTools(ep.tools);
      })
      .catch(() => alive && setLoadFailed(true));
    return () => {
      alive = false;
    };
  }, [engine]);

  const reason = (e: unknown, fallback: string) =>
    e instanceof ApiError && e.message ? e.message : fallback;

  const test = async () => {
    setBusy("test");
    setStatus(null);
    try {
      const r = await api.testAgentEndpoint(engine, {
        base_url: url.trim(),
        ...(key.trim() ? { api_key: key.trim() } : {}),
      });
      setModels(r.models);
      setStatus({
        kind: "ok",
        text:
          r.listing === "ok"
            ? `The endpoint answered — ${r.models.length} model${r.models.length === 1 ? "" : "s"} listed. Nothing was saved.`
            : "The endpoint answered but cannot list models — type the model id. Nothing was saved.",
      });
    } catch (e) {
      setStatus({ kind: "err", text: reason(e, "The test could not run.") });
    } finally {
      setBusy(null);
    }
  };

  // While a save is in flight the fields are read-only: its response is adopted as the stored
  // state, and edits made meanwhile would be silently replaced by it (Hermes on #1219).
  const save = async (patchOverride?: AgentEndpointPatch) => {
    if (!stored) return;
    const patch: AgentEndpointPatch = patchOverride ?? {};
    if (!patchOverride) {
      if (url.trim() !== stored.base_url) patch.base_url = url.trim();
      if (model.trim() !== stored.model) patch.model = model.trim();
      if (key.trim()) patch.api_key = key.trim();
      const cw = Number(context);
      if (Number.isInteger(cw) && cw !== stored.context_window) patch.context_window = cw;
      const mo = Number(output);
      if (Number.isInteger(mo) && mo !== stored.max_output_tokens) patch.max_output_tokens = mo;
      if (tools !== stored.tools) patch.tools = tools;
    }
    setBusy("save");
    setStatus(null);
    try {
      const ep = await api.setAgentEndpoint(engine, patch);
      adopt(ep);
      await reloadRoster();
      refreshConfig();
      setStatus({
        kind: "ok",
        text: ep.configured
          ? "Saved — the agent is available now."
          : "Saved. It needs a URL, a key and a model before it can start.",
      });
    } catch (e) {
      setStatus({ kind: "err", text: reason(e, "Couldn’t save the endpoint.") });
    } finally {
      setBusy(null);
    }
  };

  if (loadFailed) {
    return <p className={styles.note}>Couldn’t load this agent’s endpoint.</p>;
  }

  return (
    <form
      className={styles.card}
      aria-labelledby="agent-endpoint-h"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <h3 id="agent-endpoint-h" className={styles.h}>
        Endpoint
      </h3>
      <label className={styles.field}>
        <span>Base URL</span>
        <input
          className={styles.input}
          readOnly={busy === "save"}
          type="url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder="https://llm.example.lan/v1"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
      </label>
      <label className={styles.field}>
        <span>API key</span>
        <input
          className={styles.input}
          readOnly={busy === "save"}
          type="password"
          autoComplete="new-password"
          placeholder={
            stored?.api_key_set ? "•••••••• stored, encrypted — type to replace" : "the endpoint’s API key"
          }
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
      </label>
      <label className={styles.field}>
        <span>Model</span>
        <input
          className={styles.input}
          readOnly={busy === "save"}
          list={`agent-models-${engine}`}
          autoComplete="off"
          spellCheck={false}
          value={model}
          onChange={(e) => setModel(e.target.value)}
        />
        <datalist id={`agent-models-${engine}`}>
          {models.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
      </label>
      <div className={styles.row}>
        <label className={styles.field}>
          <span>Context window (tokens)</span>
          <input
            className={styles.input}
            readOnly={busy === "save"}
            inputMode="numeric"
            value={context}
            onChange={(e) => setContext(e.target.value)}
          />
        </label>
        <label className={styles.field}>
          <span>Max reply (tokens)</span>
          <input
            className={styles.input}
            readOnly={busy === "save"}
            inputMode="numeric"
            value={output}
            onChange={(e) => setOutput(e.target.value)}
          />
        </label>
      </div>
      <fieldset className={styles.tools}>
        <legend>Tools</legend>
        <label className={styles.choice}>
          <input
            type="radio"
            name={`agent-tools-${engine}`}
            value="none"
            checked={tools === "none"}
            onChange={() => setTools("none")}
          />
          <span>
            <b>None</b> — it can only talk.
          </span>
        </label>
        <label className={styles.choice}>
          <input
            type="radio"
            name={`agent-tools-${engine}`}
            value="read"
            checked={tools === "read"}
            onChange={() => setTools("read")}
          />
          <span>
            <b>Read files</b> in the conversation’s folder. The files it opens are sent to this
            endpoint, and its replies — which are kept — may quote them. Hidden and
            credential-shaped files (<code>.env</code>, <code>.git/</code>, keys) are refused, but
            an ordinary file can still hold a secret. It can never write, delete or run anything.
          </span>
        </label>
      </fieldset>
      <p className={styles.note}>
        The key is bound to this endpoint’s origin: a URL on another host is refused unless you
        enter a new key with it (or remove the key). <b>Save</b> stores the endpoint and makes the
        agent available at once; <b>Test</b> only checks the draft and saves nothing.
      </p>
      {status && (
        <p
          className={status.kind === "err" ? styles.err : styles.ok}
          role={status.kind === "err" ? "alert" : "status"}
          data-testid="endpoint-status"
        >
          {status.text}
        </p>
      )}
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.ghost}
          disabled={busy !== null || !url.trim()}
          onClick={() => void test()}
        >
          Test
        </button>
        <button type="submit" className={styles.primary} disabled={busy !== null || !stored}>
          Save
        </button>
        {stored?.api_key_set && (
          <button
            type="button"
            className={styles.ghost}
            disabled={busy !== null}
            onClick={() => void save({ api_key: null })}
          >
            Remove key
          </button>
        )}
      </div>
      <p className={styles.note}>
        Runtime <code>chat</code> · Tools:{" "}
        {stored?.tools === "read" ? (
          <>
            <b>read</b> — it can read files in the conversation’s folder. It cannot run commands or
            edit files.
          </>
        ) : (
          <>
            <b>none</b> — this agent can only talk. It cannot run commands or edit files.
          </>
        )}
      </p>
    </form>
  );
}
