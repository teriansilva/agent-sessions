/** The four HQ model rows retain their existing save APIs and independent field inheritance. */
import { useCallback, useEffect, useRef, useState } from "react";
import { useConfigRefresh } from "../app/config";
import { api } from "../lib/api";
import type { HqModelChoice, HqThinking } from "../types/hq";
import styles from "./Settings.module.css";

const THINKING: { value: HqThinking; label: string }[] = [
  { value: "hq", label: "Inherit" }, { value: "auto", label: "Auto · provider policy" },
  { value: "off", label: "Off" }, { value: "low", label: "Low" },
  { value: "medium", label: "Medium" }, { value: "high", label: "High" },
];
const ROWS = [
  { id: "typed", label: "Typed turns", detail: "Answers and foreground searches for messages you type to HQ." },
  { id: "route", label: "Question routing", detail: "The deciding request when your endpoint cannot use tools. Routing enablement stays below." },
  { id: "call", label: "Calls", detail: "Defaults for requests in a spoken turn. Explicit nested choices can override each field." },
  { id: "mission", label: "Mission conversations", detail: "The mission conversation only. Supervision, judging and execution keep their own policies." },
] as const;
type Row = typeof ROWS[number]["id"];
type Choices = Partial<Record<Row, HqModelChoice>>;
const isChoice = (v: unknown): v is HqModelChoice => {
  if (!v || typeof v !== "object") return false;
  const c = v as HqModelChoice;
  return typeof c.model === "string" && THINKING.some((o) => o.value === c.thinking);
};
const levelLabel = (value: string) => THINKING.find((o) => o.value === value)?.label ?? value;

export function HqModelsSettings() {
  const refresh = useConfigRefresh();
  const live = useRef(false);
  const settingsRead = useRef(0);
  const catalogRead = useRef(0);
  const writing = useRef(new Set<Row>());
  const [choices, setChoices] = useState<Choices>({});
  const [busy, setBusy] = useState<Partial<Record<Row, boolean>>>({});
  const [errors, setErrors] = useState<Partial<Record<Row, string>>>({});
  const [saved, setSaved] = useState<Partial<Record<Row, boolean>>>({});
  const [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState<string | null>(null);
  const [endpoint, setEndpoint] = useState<{ model: string; thinking: string } | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [catalogError, setCatalogError] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);

  const loadSettings = useCallback(async () => {
    if (writing.current.size) return;
    const generation = ++settingsRead.current;
    setLoading(true);
    const [cfg, call] = await Promise.allSettled([api.config(), api.voiceSettings()]);
    if (!live.current || generation !== settingsRead.current) return;
    const next: Choices = {};
    if (cfg.status === "fulfilled" && cfg.value) {
      const c = cfg.value;
      for (const row of ["typed", "mission"] as const) {
        if (isChoice(c.hq_models?.[row])) next[row] = c.hq_models[row];
      }
      if (isChoice(c.hq_route)) next.route = { model: c.hq_route.model, thinking: c.hq_route.thinking };
      setEndpoint({ model: c.ai_review?.model || "Configured model", thinking: c.ai_review?.thinking || "auto" });
    } else setEndpoint(null);
    if (call.status === "fulfilled") {
      const c = { model: call.value?.hq_model, thinking: call.value?.thinking };
      if (isChoice(c)) next.call = c;
    }
    setChoices(next);
    setErrors({});
    setSaved({});
    setReadError(ROWS.every((row) => next[row.id]) ? null : "Some HQ model settings could not be read. Reload settings before editing those rows.");
    setLoading(false);
  }, []);

  const loadModels = useCallback(async (fresh = false) => {
    const generation = ++catalogRead.current;
    setCatalogLoading(true);
    try {
      const result = await api.aiReviewModels(fresh ? { refresh: true } : undefined);
      if (!Array.isArray(result?.models)) throw new Error("No model list");
      if (live.current && generation === catalogRead.current) {
        setModels(result.models.filter((m) => typeof m === "string" && m));
        setCatalogError(false);
      }
    } catch {
      if (live.current && generation === catalogRead.current) setCatalogError(true);
    } finally {
      if (live.current && generation === catalogRead.current) setCatalogLoading(false);
    }
  }, []);

  useEffect(() => {
    live.current = true;
    void loadSettings();
    void loadModels();
    // A new mount/read advances its generation; an unmount suppresses every late response.
    return () => { live.current = false; };
  }, [loadSettings, loadModels]);

  const save = async (row: Row, patch: Partial<HqModelChoice>) => {
    if (writing.current.has(row) || loading || !choices[row]) return;
    writing.current.add(row);
    setBusy((c) => ({ ...c, [row]: true }));
    setErrors((c) => ({ ...c, [row]: "" }));
    setSaved((c) => ({ ...c, [row]: false }));
    try {
      let accepted: unknown;
      if (row === "call") {
        const response = await api.saveVoiceSettings({
          ...(patch.model !== undefined ? { hq_model: patch.model } : {}),
          ...(patch.thinking !== undefined ? { thinking: patch.thinking } : {}),
        });
        accepted = { model: response.hq_model, thinking: response.thinking };
      } else {
        const response = await api.setPrefs(row === "route"
          ? { hq_route: patch } : { hq_models: { [row]: patch } });
        accepted = row === "route" ? response.hq_route
          : (response.hq_models as Choices | undefined)?.[row];
      }
      if (!isChoice(accepted)) throw new Error("The saved settings could not be read. Reload settings before trying again.");
      if (live.current) {
        const choice = accepted;
        setChoices((c) => ({ ...c, [row]: choice }));
        setSaved((c) => ({ ...c, [row]: true }));
      }
      refresh();
    } catch (e) {
      if (live.current) setErrors((c) => ({ ...c, [row]: e instanceof Error ? e.message : "Couldn’t save. Try again." }));
    } finally {
      writing.current.delete(row);
      if (live.current) setBusy((c) => ({ ...c, [row]: false }));
    }
  };

  const source = (row: Row, field: keyof HqModelChoice) => {
    const own = choices[row]?.[field];
    if (own && (field === "model" || own !== "hq")) return `Own choice · ${field === "thinking" ? levelLabel(own) : own}`;
    const root = !endpoint ? "unavailable until settings reload"
      : field === "thinking" ? levelLabel(endpoint.thinking) : endpoint.model;
    if (row === "route" || row === "mission") {
      const call = choices.call?.[field];
      const inherited = call && (field === "model" || call !== "hq") ? (field === "thinking" ? levelLabel(call) : call) : root;
      return `From the enclosing call (${inherited}) when on a call; otherwise Endpoint & model (${root}).`;
    }
    return `From Endpoint & model · ${root}`;
  };

  return (
    <section className={styles.section} aria-labelledby="hq-models-h" data-testid="hq-models-settings">
      <h2 id="hq-models-h">HQ models</h2>
      <p className={styles.hint}>All four rows use the shared AI endpoint. Model and thinking inherit independently. Changes save automatically.</p>
      {readError && <div role="alert" className={styles.err}>
        <p>{readError}</p>
        <button className={styles.secBtnGhost} disabled={loading || Object.values(busy).some(Boolean)} onClick={() => void loadSettings()}>Reload settings</button>
      </div>}
      {catalogError && <p className={styles.hint}>Model list unavailable. Saved choices are preserved.</p>}
      <button type="button" className={styles.secBtnGhost} disabled={catalogLoading} onClick={() => void loadModels(true)}>
        {catalogLoading ? "Reading model list…" : "Reload model list"}
      </button>
      <div className={styles.modelRows}>
        {ROWS.map(({ id, label, detail }) => {
          const value = choices[id];
          return <fieldset key={id} className={styles.modelRow} disabled={loading || busy[id] || !value}>
            <legend>{label}</legend>
            <p className={styles.hint}>{detail}</p>
            <div className={styles.modelGrid}>
              <div className={styles.aiField}>
                <label className={styles.aiFieldLabel} htmlFor={`hq-model-${id}`}>Model for {label.toLowerCase()}</label>
                <select id={`hq-model-${id}`} className={styles.aiInput} value={value?.model ?? ""}
                  onChange={(e) => void save(id, { model: e.currentTarget.value })}
                  data-testid={id === "route" ? "hq-route-model" : id === "call" ? "hq-calls-model" : undefined}>
                  <option value="">Inherit</option>
                  {[...new Set([...(value?.model ? [value.model] : []), ...models])].map((model) => <option key={model} value={model}>{model}</option>)}
                </select>
                <p className={styles.modelSource}>{source(id, "model")}</p>
              </div>
              <div className={styles.aiField}>
                <label className={styles.aiFieldLabel} htmlFor={`hq-thinking-${id}`}>Thinking for {label.toLowerCase()}</label>
                <select id={`hq-thinking-${id}`} className={styles.aiInput} value={value?.thinking ?? "hq"}
                  onChange={(e) => void save(id, { thinking: e.currentTarget.value as HqThinking })}
                  data-testid={id === "route" ? "hq-route-thinking" : id === "call" ? "hq-calls-thinking" : undefined}>
                  {THINKING.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                </select>
                <p className={styles.modelSource}>{source(id, "thinking")}</p>
              </div>
            </div>
            {errors[id] && <div role="alert" className={styles.err}>
              <p>{errors[id]}</p>
              <button type="button" className={styles.secBtnGhost} disabled={Object.values(busy).some(Boolean)} onClick={() => void loadSettings()}>Reload settings</button>
            </div>}
            {saved[id] && <p role="status" className={styles.ok}>Saved.</p>}
          </fieldset>;
        })}
      </div>
    </section>
  );
}
