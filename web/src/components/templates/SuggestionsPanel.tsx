import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Sparkles, X } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import { errMessage } from "../../routes/templatesLib";
import { settingsPath } from "../../routes/settingsTabs";
import type {
  TemplateDraft,
  TemplateSuggestion,
  TemplateSuggestionsResult,
} from "../../types/api";
import page from "../../routes/Templates.module.css";
import styles from "./SuggestionsPanel.module.css";

/** A suggestion's template draft in the editor's terms: every slot is a template field. */
function suggestionDraft(s: Extract<TemplateSuggestion, { kind: "template" }>): TemplateDraft {
  return {
    name: s.name,
    description: s.reason,
    body: s.body,
    fields: s.fields.map((f) => ({ ...f, source: "template" as const, kind: "text" as const })),
  };
}

/** SUGGESTED — "what template should I write?" (#1090, Phase 3).
 *
 *  ANALYSE sends what the operator typed to their agents in the last 30 days — never the agents'
 *  replies, stored secrets and credential-shaped text removed first — to their own AI endpoint,
 *  and shows what it proposes: templates for instructions they keep retyping, variables for
 *  values they keep pasting. It runs ONLY when pressed. Every suggestion is a draft: OPEN IN
 *  EDITOR and ADD TO LIBRARY prefill the ordinary forms, and nothing is saved until the operator
 *  saves it there. A variable that looks like a credential arrives with no value — it is typed
 *  into the new-secret form, never echoed back.
 *
 *  STOP WAITING only stops this page waiting (the request is aborted client-side); the server may
 *  still finish, and then the result is here next time. A failed analysis keeps the previous
 *  result, and says why. */

export function SuggestionsPanel({
  onOpenTemplate,
  onAddVariable,
  onCountChange,
}: {
  /** OPEN IN EDITOR, and a written draft: it becomes a new template's prefill. */
  onOpenTemplate: (draft: TemplateDraft) => void;
  /** ADD TO LIBRARY: the draft becomes the new-variable (or new-secret) form's prefill. */
  onAddVariable: (s: Extract<TemplateSuggestion, { kind: "variable" }>) => void;
  onCountChange?: (n: number | null) => void;
}) {
  const [result, setResult] = useState<TemplateSuggestionsResult | null>(null);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const [request, setRequest] = useState("");
  const [writing, setWriting] = useState(false);
  const [writeError, setWriteError] = useState("");
  const writeAbort = useRef<AbortController | null>(null);
  // Which analysis is on screen. A dismiss's rollback belongs to the result it removed a card
  // from: after a newer analysis replaced that result, putting the card back would add it to a
  // list it is not in — or twice (Hermes on #1110).
  const generation = useRef(0);

  useEffect(() => {
    let alive = true;
    api
      .templateSuggestions()
      .then((r) => {
        if (!alive) return;
        setResult(r.result);
        setConfigured(r.configured);
        setLoaded(true);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setError(errMessage(e, "Could not load suggestions"));
        setLoaded(true);
      });
    return () => {
      alive = false;
      abortRef.current?.abort();
      writeAbort.current?.abort();
    };
  }, []);

  // The tab's count follows what is on screen — one source, so a dismiss can never report a
  // count from the result it just replaced.
  useEffect(() => {
    if (loaded) onCountChange?.(result ? result.suggestions.length : null);
  }, [result, loaded, onCountChange]);

  const analyse = async () => {
    if (running) return;
    const ctl = new AbortController();
    abortRef.current = ctl;
    setRunning(true);
    setError("");
    setNote("");
    try {
      const r = await api.suggestTemplates(ctl.signal);
      generation.current += 1;
      setResult(r);
    } catch (e) {
      if (ctl.signal.aborted) {
        setNote(
          "Stopped waiting — if the analysis finishes, its suggestions will be here next time.",
        );
      } else if (e instanceof ApiError && e.status === 409 && /No AI endpoint/i.test(e.message)) {
        setConfigured(false);
      } else {
        setError(errMessage(e, "Analysis failed"));
      }
    } finally {
      if (abortRef.current === ctl) abortRef.current = null;
      setRunning(false);
    }
  };

  const stop = () => abortRef.current?.abort();

  // WRITE ME A TEMPLATE FOR … — one draft from what the operator describes, straight into the
  // editor. Independent of ANALYSE: it needs no analysis, and reads no transcript.
  const write = async (e: React.FormEvent) => {
    e.preventDefault();
    if (writing || !request.trim()) return;
    const ctl = new AbortController();
    writeAbort.current = ctl;
    setWriting(true);
    setWriteError("");
    try {
      const r = await api.writeTemplate(request.trim(), ctl.signal);
      if (!ctl.signal.aborted) onOpenTemplate(r.template); // not after the tab was left
    } catch (err) {
      if (!ctl.signal.aborted) {
        if (err instanceof ApiError && err.status === 409 && /No AI endpoint/i.test(err.message)) {
          setConfigured(false);
        } else {
          setWriteError(errMessage(err, "Could not write the template"));
        }
      }
    } finally {
      if (writeAbort.current === ctl) writeAbort.current = null;
      setWriting(false);
    }
  };

  const dismiss = async (s: TemplateSuggestion) => {
    // Optimistic: a dismissed card leaves now; a failed dismiss puts it back and says so — into
    // the same result it left, never into a newer one, and never twice.
    const gen = generation.current;
    setResult((r) => (r ? { ...r, suggestions: r.suggestions.filter((x) => x.id !== s.id) } : r));
    try {
      await api.dismissSuggestion(s.id);
    } catch (e) {
      if (generation.current === gen) {
        setResult((r) =>
          r && !r.suggestions.some((x) => x.id === s.id)
            ? { ...r, suggestions: [s, ...r.suggestions] }
            : r,
        );
      }
      setError(errMessage(e, "Could not dismiss the suggestion"));
    }
  };

  if (!loaded) return <p className={page.state}>Loading…</p>;

  const stats = result?.stats ?? {};
  const noEndpoint = configured === false;

  return (
    <section aria-label="Suggested templates">
      {!noEndpoint && (
        <form className={styles.write} onSubmit={(e) => void write(e)} aria-label="Write a template">
          <label className={styles.writeLabel} htmlFor="tpl-write-request">
            Write me a template for…
          </label>
          <div className={styles.writeRow}>
            <textarea
              id="tpl-write-request"
              className={styles.writeInput}
              rows={2}
              maxLength={2000}
              value={request}
              placeholder="e.g. reviewing a pull request against our coding guidelines"
              onChange={(e) => setRequest(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void write(e);
              }}
              disabled={writing}
            />
            <button
              type="submit"
              className={page.cta}
              disabled={writing || !request.trim()}
            >
              <Sparkles size={14} aria-hidden="true" />
              {writing ? "Writing…" : "Write it"}
            </button>
          </div>
          <p className={styles.writeHint}>
            Only this request and the names of your templates and variables are sent. The draft
            opens in the editor; nothing is saved until you save it.
          </p>
          {writeError && (
            <p className={page.err} role="alert">
              {writeError}
            </p>
          )}
        </form>
      )}
      {note && <p className={page.note}>{note}</p>}
      {error && (
        <p className={page.err} role="alert">
          {error}
          {result ? " Your previous suggestions are kept below." : ""}
        </p>
      )}

      {running && (
        <div className={styles.status} role="status">
          <span className={styles.square} aria-hidden="true" />
          <div className={styles.statusText}>
            <strong>Analysing your recent messages…</strong>
            <span>This can take up to a minute.</span>
          </div>
          <button type="button" className={page.ghost} onClick={stop}>
            Stop waiting
          </button>
        </div>
      )}

      {noEndpoint && !running && (
        <div className={`${styles.status} ${styles.statusWarn}`}>
          <div className={styles.statusText}>
            <strong>No AI endpoint set up</strong>
            <span>Suggestions need the AI endpoint in Settings → AI. Nothing was sent.</span>
          </div>
          <Link to={settingsPath("ai-endpoint")} className={page.cta}>
            Open settings
          </Link>
        </div>
      )}

      {!result && !running && !noEndpoint && (
        <div className={page.empty}>
          <span className={page.emptyGlyph} aria-hidden="true">
            ✦
          </span>
          <p className={page.emptyTitle}>What template should you write?</p>
          <p className={page.emptyHint}>
            Analyse reads what <strong>you</strong> typed to your agents in the last 30 days — never
            their replies — and proposes templates for what you keep retyping and variables for
            values you keep pasting. Stored secrets are removed, and a message that looks like it
            holds a password or token is left out entirely.
          </p>
          <div className={page.emptyActs}>
            <button type="button" className={page.cta} onClick={() => void analyse()}>
              <Sparkles size={14} aria-hidden="true" />
              Analyse my messages
            </button>
          </div>
        </div>
      )}

      {result && !running && (
        <>
          <div className={styles.meta}>
            <span>
              From {stats.messages ?? 0} {stats.messages === 1 ? "message" : "messages"} ·{" "}
              {stats.sessions ?? 0} {stats.sessions === 1 ? "session" : "sessions"} · last{" "}
              {stats.days ?? 30} days
              {(stats.withheld ?? 0) > 0 &&
                ` · ${stats.withheld} left out because they looked like they held a password or token`}
            </span>
            {!noEndpoint && (
              <button type="button" className={page.ghost} onClick={() => void analyse()}>
                <Sparkles size={13} aria-hidden="true" />
                Analyse again
              </button>
            )}
          </div>
          {result.suggestions.length === 0 ? (
            <p className={page.state}>
              Nothing you type repeats enough to be worth a template yet — or you already have one
              for it.
            </p>
          ) : (
            <ul className={styles.cards} aria-label="Suggestions">
              {result.suggestions.map((s) => (
                <li key={s.id} className={styles.card} data-suggestion={s.id}>
                  <div className={styles.cardHead}>
                    <span className={styles.kind}>
                      {s.kind === "template" ? "Template" : "Variable"}
                    </span>
                    <h3 className={styles.name}>
                      {s.kind === "variable" ? `{{${s.name}}}` : s.name}
                    </h3>
                  </div>
                  {s.reason && <p className={styles.reason}>{s.reason}</p>}
                  {s.count > 0 && (
                    <p className={styles.count}>
                      would have replaced {s.count} {s.count === 1 ? "message" : "messages"}
                    </p>
                  )}
                  {s.kind === "template" ? (
                    <>
                      <pre className={styles.body}>{s.body}</pre>
                      {s.fields.length > 0 && (
                        <div className={styles.chips}>
                          {s.fields.map((f) => (
                            <span key={f.name} className={styles.chip}>
                              {`{{${f.name}}}`}
                            </span>
                          ))}
                        </div>
                      )}
                    </>
                  ) : s.secret ? (
                    <p className={styles.secret}>
                      🔒 Looks like a credential — its value is not kept. You type it in when you
                      add it.
                    </p>
                  ) : (
                    <pre className={styles.body}>{s.value}</pre>
                  )}
                  <div className={styles.acts}>
                    {s.kind === "template" ? (
                      <button
                        type="button"
                        className={page.cta}
                        onClick={() => onOpenTemplate(suggestionDraft(s))}
                        aria-label={`Open ${s.name} in the editor`}
                      >
                        Open in editor
                      </button>
                    ) : (
                      <button
                        type="button"
                        className={page.cta}
                        onClick={() => onAddVariable(s)}
                        aria-label={`Add ${s.name} to the library`}
                      >
                        {s.secret ? "Add as secret" : "Add to library"}
                      </button>
                    )}
                    <button
                      type="button"
                      className={page.ghost}
                      onClick={() => void dismiss(s)}
                      aria-label={`Dismiss ${s.name}`}
                    >
                      <X size={13} aria-hidden="true" />
                      Dismiss
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {result.dropped > 0 && (
            <p className={styles.dropped}>
              {result.dropped} {result.dropped === 1 ? "proposal was" : "proposals were"} left out —
              not valid as a template or variable, or one you already have.
            </p>
          )}
        </>
      )}
    </section>
  );
}
