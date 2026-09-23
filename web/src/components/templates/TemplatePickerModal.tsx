import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import { Search, Send, X } from "lucide-react";
import { api } from "../../lib/api";
import {
  missingLibrary,
  missingRequired,
  renderTemplate,
  seedValues,
  type LibraryValues,
} from "../../lib/templateMessage";
import { errMessage } from "../../routes/templatesLib";
import type { Template } from "../../types/api";
import { useInertBehind } from "./useInertBehind";
import styles from "./TemplatePickerModal.module.css";

/** The composer's template picker (#905 P3): pick a template, fill its `{{fields}}`, then
 *  INSERT it into the composer (the #619 Restore semantics — the operator presses Send) or SEND
 *  it as one message through the composer's own `sendPayload` seam.
 *
 *  Same vocabulary as `SentMessagesModal` (portal to <body>, `role="dialog"`, Esc/backdrop close,
 *  bottom sheet ≤800px). The library is fetched on open — it is small and the chip must not
 *  cost a request per session pane. The preview under the fill step is `renderTemplate`, the
 *  same assembly the editor's preview and the composer's send use, so what it shows is what
 *  SEND pastes.
 *
 *  Library fields (#1090) start at the variables library's value and stay editable for this one
 *  send; the library is not changed. A library field whose variable does not exist disables
 *  Send AND Insert — its slot would otherwise go out empty. The library is fetched beside the
 *  templates; if it cannot be loaded the picker still works, every library field reads as
 *  missing, and it says why. */

export type FieldValues = Record<string, string>;

const NO_LIBRARY: LibraryValues = {};

function matches(t: Template, q: string): boolean {
  if (!q) return true;
  return `${t.name}\n${t.description}\n${t.tags.join(" ")}\n${t.body}`.toLowerCase().includes(q);
}

export function TemplatePickerModal({
  preselect,
  onInsert,
  onSend,
  insertLabel = "Insert into composer",
  onClose,
  onOpenGallery,
  returnFocusTo,
}: {
  /** Open with this template already selected (the gallery's USE lands here). */
  preselect?: string;
  onInsert: (t: Template, values: FieldValues) => void;
  /** Send the template as one message. OPTIONAL (#948): the mission composer inserts a template
   *  into its brief but has no session to send into, so it omits this and the picker offers
   *  Insert only. */
  onSend?: (t: Template, values: FieldValues) => void;
  /** The Insert button's wording — "Insert into composer" by default. */
  insertLabel?: string;
  onClose: () => void;
  /** The gallery links, as an in-app navigation (the composer flushes its draft first). Without
   *  it the links are plain anchors — the picker never assumes a router (#908 round 4). */
  onOpenGallery?: (to: string) => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const [templates, setTemplates] = useState<Template[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(preselect ?? null);
  const [values, setValues] = useState<FieldValues>({});
  // `null` until loaded; a failed load is an empty library plus `libraryError`, never a blocker.
  const [library, setLibrary] = useState<LibraryValues | null>(null);
  const [libraryError, setLibraryError] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const titleId = "template-picker-title";

  useEffect(() => {
    let alive = true;
    const vars = api
      .templateVariables()
      .then((r): LibraryValues => Object.fromEntries(r.variables.map((v) => [v.name, v.value])))
      .catch((): LibraryValues => {
        if (alive) setLibraryError(true);
        return NO_LIBRARY;
      });
    Promise.all([api.templates(), vars])
      .then(([r, lib]) => {
        if (!alive) return;
        setTemplates(r.templates);
        setLibrary(lib);
        const pre = preselect ? r.templates.find((t) => t.id === preselect) : undefined;
        if (pre) setValues(seedValues(pre.fields, lib));
        else if (preselect) setSelectedId(null);
      })
      .catch((e: unknown) => {
        if (alive) setError(errMessage(e, "Could not load templates"));
      });
    return () => {
      alive = false;
    };
  }, [preselect]);

  // Focus lands INSIDE the dialog from the first paint — `#root` is inert while it is open, so
  // anything left outside is unreachable. The search input exists only once the library has
  // loaded and is non-empty, so the always-present Close takes focus first and the input takes
  // over when it mounts (Hermes on #908).
  const closeRef = useRef<HTMLButtonElement>(null);
  const hasSearch = templates !== null && templates.length > 0;
  useEffect(() => {
    if (hasSearch) searchRef.current?.focus();
    else closeRef.current?.focus();
  }, [hasSearch]);
  useInertBehind(returnFocusTo);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (templates ?? []).filter((t) => matches(t, q));
  }, [templates, query]);
  const selected = templates?.find((t) => t.id === selectedId) ?? null;
  const missing = selected ? missingRequired(selected.fields, values) : [];
  const unset = selected ? missingLibrary(selected.fields, library ?? NO_LIBRARY) : [];
  const blocked = unset.length > 0;
  const preview = selected ? renderTemplate(selected, values) : "";

  const gallery = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!onOpenGallery) return; // no router to hand off to: the anchor navigates
    e.preventDefault();
    onOpenGallery("/templates");
  };

  const variablesLink = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!onOpenGallery) return;
    e.preventDefault();
    onOpenGallery("/templates?tab=variables");
  };

  const select = (t: Template) => {
    if (t.id === selectedId) {
      setSelectedId(null);
      return;
    }
    setSelectedId(t.id);
    setValues(seedValues(t.fields, library ?? NO_LIBRARY));
  };

  return createPortal(
    <div className={styles.backdrop} onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={styles.dialog}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={styles.head}>
          <span id={titleId} className={styles.tag}>
            Templates // pick one
          </span>
          <button
            ref={closeRef}
            type="button"
            className={styles.close}
            aria-label="Close"
            onClick={onClose}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>

        {templates !== null && templates.length > 0 && (
          <label className={styles.search}>
            <Search size={14} aria-hidden="true" />
            <input
              ref={searchRef}
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search…"
              aria-label="Search templates"
            />
          </label>
        )}

        {error && (
          <p className={styles.err} role="alert">
            {error}
          </p>
        )}
        {templates === null && !error && <p className={styles.state}>Loading…</p>}
        {templates !== null && templates.length === 0 && (
          <p className={styles.state}>
            No templates yet —{" "}
            <a href="/templates" onClick={gallery}>
              create one in the gallery
            </a>
            .
          </p>
        )}
        {templates !== null && templates.length > 0 && visible.length === 0 && (
          <p className={styles.state}>No template matches</p>
        )}

        {visible.length > 0 && (
          <ul className={styles.list} aria-label="Templates">
            {visible.map((t) => {
              const on = t.id === selectedId;
              return (
                <li key={t.id} className={`${styles.row} ${on ? styles.rowOn : ""}`}>
                  <button
                    type="button"
                    className={styles.pick}
                    aria-pressed={on}
                    onClick={() => select(t)}
                  >
                    <span className={styles.name}>{t.name}</span>
                    <span className={styles.sub}>
                      {[
                        t.fields.length
                          ? `${t.fields.length} ${t.fields.length === 1 ? "field" : "fields"}`
                          : null,
                        t.images.length
                          ? `${t.images.length} ${t.images.length === 1 ? "image" : "images"}`
                          : null,
                      ]
                        .filter(Boolean)
                        .join(" · ") || "no fields"}
                      {t.description ? ` · ${t.description}` : ""}
                    </span>
                  </button>
                  {on && selected && (
                    <div className={styles.fill}>
                      {selected.fields.map((f) => (
                        <label key={f.name} className={styles.field}>
                          <span className={styles.flabel}>
                            {f.label}
                            {f.required ? " · required" : ""}
                            {f.source === "library"
                              ? unset.includes(f.name)
                                ? " · missing library variable"
                                : " · from library"
                              : !f.required && f.default
                                ? ` · default “${f.default}”`
                                : ""}
                          </span>
                          {/* A textarea, never an <input>: a field value may span lines (a
                              default or a library value both may), and an input silently
                              drops the newlines — `cd repo⏎npm test` would send as
                              `cd reponpm test` (Hermes on #1095). */}
                          <textarea
                            className={styles.input}
                            value={values[f.name] ?? ""}
                            rows={Math.min(6, Math.max(1, (values[f.name] ?? "").split("\n").length))}
                            onChange={(e) =>
                              setValues((v) => ({ ...v, [f.name]: e.target.value }))
                            }
                            aria-label={f.label}
                            autoComplete="off"
                            spellCheck={false}
                          />
                        </label>
                      ))}
                      <pre className={styles.preview} aria-label="What will be sent">
                        {preview}
                      </pre>
                      {blocked && (
                        <p className={styles.blocked} role="alert">
                          {libraryError
                            ? "Couldn't load the variables library, so "
                            : "The variables library has no "}
                          {libraryError ? (
                            <>
                              {unset.map((n) => `{{${n}}}`).join(", ")}{" "}
                              {unset.length === 1 ? "has" : "have"} no value.
                            </>
                          ) : (
                            <>
                              {unset.map((n) => `{{${n}}}`).join(", ")} — add{" "}
                              {unset.length === 1 ? "it" : "them"} under{" "}
                              <a href="/templates?tab=variables" onClick={variablesLink}>
                                Variables
                              </a>{" "}
                              first.
                            </>
                          )}
                        </p>
                      )}
                      <div className={styles.acts}>
{onSend ? (
                        <button
                          type="button"
                          className={`${styles.act} ${styles.primary}`}
                          disabled={missing.length > 0 || blocked}
                          title={
                            blocked
                              ? "A library variable this template uses does not exist"
                              : missing.length
                                ? `Fill ${missing.length === 1 ? "the required field" : "the required fields"} first`
                                : "Send now, as one message"
                          }
                          aria-label={`Send ${t.name}`}
                          onClick={() => onSend?.(selected, values)}
                        >
                          <Send size={12} aria-hidden="true" />
                          Send
                        </button>
                        ) : null}
                        <button
                          type="button"
                          className={styles.act}
                          aria-label={`Insert ${t.name} into ${onSend ? "composer" : "mission brief"}`}
                          disabled={blocked}
                          onClick={() => onInsert(selected, values)}
                        >
                          {insertLabel}
                        </button>
                      </div>
                      {onSend ? (
                        <p className={styles.note}>
                          Sent as one message · recorded in sent history like any other
                        </p>
                      ) : null}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        <p className={styles.foot}>
          <a href="/templates" onClick={gallery}>
            Manage in the gallery
          </a>
        </p>
      </div>
    </div>,
    document.body,
  );
}
