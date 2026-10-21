import { useCallback, useEffect, useRef, useState } from "react";
import { useConfigRefresh } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { PromptEntry } from "../types/api";
import styles from "./Settings.module.css";

/** The hash that deep-links one row: /settings/ai-review#prompt-session_recap. */
const HASH_PREFIX = "#prompt-";

type RowState = { draft: string; error: string | null; saving: boolean };

/** Prompt catalog (#824) — every system prompt this app sends, editable in one place.
 *
 *  Rendered entirely from `GET /api/prompts`: a prompt added to the server-side registry
 *  shows up here with no change to this file, which is the point of the catalog. Rows are
 *  collapsed by default (eleven expanded editors would bury the rest of the AI tab) and each
 *  one carries the JSON contract its caller parses — the single thing an edit can break.
 *
 *  Save semantics are deliberate and NOT commit-on-blur (unlike the endpoint fields above):
 *  typing edits a local draft, nothing is written until Save. `Reset to default` persists
 *  immediately — one tap to get the shipped prompt back, the same as the AI-review reset it
 *  replaces. A failed save keeps the draft, so a network blip never eats an edit. */
export function PromptsSettings() {
  const refreshConfig = useConfigRefresh();
  const [rows, setRows] = useState<PromptEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [state, setState] = useState<Record<string, RowState>>({});
  const rowRefs = useRef<Record<string, HTMLDivElement | null>>({});

  // Promise-chain shape (as PushDevices/AutoSort do): every setState lands in a `.then`,
  // never synchronously inside the effect, so mounting cannot cascade a render.
  const load = useCallback(() => {
    Promise.resolve()
      .then(() => api.prompts())
      .then((r) => {
        setLoadError(null);
        setRows(r.prompts);
        setState(
          Object.fromEntries(
            r.prompts.map((p) => [p.id, { draft: p.value, error: null, saving: false }]),
          ),
        );
      })
      .catch((e) => {
        setRows(null);
        setLoadError(e instanceof ApiError ? e.message : "Could not load prompts.");
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Deep link: arriving on (or clicking through to) #prompt-<id> opens that row and brings it
  // into view. Runs once the catalog has landed — the hash is resolved against real ids, not
  // before they exist — and again on hashchange, which is how the "edit it here" links in the
  // panels above land on the right row.
  useEffect(() => {
    if (!rows) return;
    const openFromHash = () => {
      const hash = window.location.hash;
      if (!hash.startsWith(HASH_PREFIX)) return;
      const id = hash.slice(HASH_PREFIX.length);
      if (!rows.some((p) => p.id === id)) return;
      setOpen(id);
      // Opening the row is the behaviour; scrolling to it is polish, and not every
      // environment implements it (jsdom does not) — so never let it break the open.
      rowRefs.current[id]?.scrollIntoView?.({ block: "center" });
    };
    openFromHash();
    window.addEventListener("hashchange", openFromHash);
    return () => window.removeEventListener("hashchange", openFromHash);
  }, [rows]);

  const patch = (id: string, next: Partial<RowState>) =>
    setState((s) => ({ ...s, [id]: { ...s[id], ...next } }));

  async function commit(p: PromptEntry, action: "save" | "reset") {
    // The textarea stays editable while the request is in flight (blocking it would be worse —
    // a slow endpoint would eat keystrokes), so the response can land AFTER the operator has
    // typed more. `before` is what was in the box when the request started: the draft is
    // canonicalized to the server's answer only when it has not moved since. NOTE this is not
    // the same as "what we sent" — a Reset sends the default while the box still holds the old
    // edited text, so comparing against the sent value would call every Reset superseded and
    // leave the stale text on screen.
    const before = state[p.id].draft;
    const submitted = action === "reset" ? p.default : before;
    patch(p.id, { saving: true, error: null });
    try {
      const saved =
        action === "reset" ? await api.resetPrompt(p.id) : await api.savePrompt(p.id, submitted);
      setRows((rs) => rs?.map((r) => (r.id === saved.id ? saved : r)) ?? rs);
      setState((st) => {
        const cur = st[p.id];
        const superseded = cur.draft !== before;
        return {
          ...st,
          [p.id]: {
            draft: superseded ? cur.draft : saved.value,
            saving: false,
            error: null,
          },
        };
      });
      // The three legacy prompts still ride in /api/config for the panels above; refresh so a
      // remount there cannot show what this panel just replaced.
      void refreshConfig();
    } catch (e) {
      patch(p.id, {
        saving: false,
        error:
          e instanceof ApiError
            ? e.message
            : action === "reset"
              ? "Reset failed — the prompt is unchanged."
              : "Save failed — the server did not respond. Your edit is still here.",
      });
    }
  }

  return (
    <section className={styles.section} aria-labelledby="ai-prompts-h">
      <h2 id="ai-prompts-h">Prompts</h2>
      <p className={styles.hint}>
        Every system prompt this app sends to your AI endpoint, in one place. Editing one
        changes <em>what</em> the model is asked for — never <em>where</em> the request goes.
      </p>
      <p className={styles.promptNote}>
        Each prompt must keep returning the JSON shape shown above its editor. If a reply stops
        matching, that feature falls back to its no-answer state — nothing breaks, and{" "}
        <strong>Reset to default</strong> puts the shipped prompt back.
      </p>

      {loadError !== null ? (
        <div className={styles.promptError} role="alert">
          <b>Could not load prompts.</b> The app is running and your prompts are unchanged —
          only this list failed to load.
          <div className={styles.aiActions}>
            <button type="button" className={styles.secBtnGhost} onClick={() => load()}>
              Retry
            </button>
          </div>
        </div>
      ) : rows === null ? (
        <p className={styles.hint} aria-busy="true">
          Loading prompts…
        </p>
      ) : (
        rows.map((p, i) => {
          const st = state[p.id] ?? { draft: p.value, error: null, saving: false };
          // Code points, not UTF-16 units: the server caps `len(value)` in Python, where an
          // astral character (emoji, some CJK extensions) is ONE. `"".length` counts it as two,
          // so a prompt the server accepts would show as double-length and disable Save.
          const length = [...st.draft].length;
          const over = length - p.max_chars;
          const dirty = st.draft !== p.value;
          const expanded = open === p.id;
          return (
            <div
              key={p.id}
              ref={(el) => {
                rowRefs.current[p.id] = el;
              }}
            >
              {(i === 0 || rows[i - 1].group !== p.group) && (
                <p className={styles.promptGroup}>{p.group}</p>
              )}
              <div className={styles.promptRow} id={`prompt-${p.id}`}>
                <button
                  type="button"
                  className={styles.promptToggle}
                  aria-expanded={expanded}
                  aria-controls={`prompt-body-${p.id}`}
                  onClick={() => setOpen(expanded ? null : p.id)}
                >
                  <span className={styles.promptChev} aria-hidden="true">
                    {expanded ? "▾" : "▸"}
                  </span>
                  <span className={styles.promptMain}>
                    <span className={styles.promptTitle}>{p.label}</span>
                    <span className={styles.promptDesc}>{p.description}</span>
                  </span>
                  {!p.is_default && <span className={styles.promptBadge}>Edited</span>}
                  {p.guarded && (
                    <span className={`${styles.promptBadge} ${styles.promptBadgeGuard}`}>
                      Guarded
                    </span>
                  )}
                </button>

                {expanded && (
                  <div id={`prompt-body-${p.id}`}>
                    <p className={styles.promptContract}>must return {p.contract}</p>
                    <label className={styles.aiFieldLabel} htmlFor={`prompt-ta-${p.id}`}>
                      Prompt
                      <span className={styles.promptState}>
                        {dirty ? "Unsaved" : p.is_default ? "Default" : "Edited"}
                      </span>
                    </label>
                    <textarea
                      id={`prompt-ta-${p.id}`}
                      className={`${styles.aiInput} ${styles.aiPrompt} ${
                        over > 0 ? styles.promptOver : ""
                      }`}
                      aria-label={`${p.label} prompt`}
                      aria-invalid={over > 0}
                      value={st.draft}
                      onChange={(e) => patch(p.id, { draft: e.target.value, error: null })}
                    />
                    {p.guard_suffix && (
                      <>
                        <p className={styles.promptLockLabel}>Always appended — not editable</p>
                        <p className={styles.promptLocked}>{p.guard_suffix}</p>
                      </>
                    )}
                    {over > 0 && (
                      <p className={styles.promptInlineError}>
                        Too long by {over} character{over === 1 ? "" : "s"}. Trim it, or press
                        Reset to default.
                      </p>
                    )}
                    {st.error && (
                      <p className={styles.promptInlineError} role="alert">
                        {st.error}
                      </p>
                    )}
                    <div className={styles.aiActions}>
                      <button
                        type="button"
                        className={`${styles.secBtn} shine`}
                        disabled={!dirty || over > 0 || st.saving}
                        onClick={() => void commit(p, "save")}
                      >
                        Save
                      </button>
                      <button
                        type="button"
                        className={styles.secBtnGhost}
                        disabled={p.is_default || st.saving}
                        onClick={() => void commit(p, "reset")}
                      >
                        Reset to default
                      </button>
                      <span
                        className={`${styles.promptCount} ${over > 0 ? styles.promptOverText : ""}`}
                      >
                        {length} / {p.max_chars}
                      </span>
                    </div>
                  </div>
                )}
              </div>
            </div>
          );
        })
      )}
    </section>
  );
}
