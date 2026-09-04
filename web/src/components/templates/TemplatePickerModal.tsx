import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import { Search, Send, X } from "lucide-react";
import { api } from "../../lib/api";
import { missingRequired, renderTemplate } from "../../lib/templateMessage";
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
 *  SEND pastes. */

export type FieldValues = Record<string, string>;

function seed(t: Template): FieldValues {
  const v: FieldValues = {};
  for (const f of t.fields) v[f.name] = f.default ?? "";
  return v;
}

function matches(t: Template, q: string): boolean {
  if (!q) return true;
  return `${t.name}\n${t.description}\n${t.tags.join(" ")}\n${t.body}`.toLowerCase().includes(q);
}

export function TemplatePickerModal({
  preselect,
  onInsert,
  onSend,
  onClose,
  onOpenGallery,
  returnFocusTo,
}: {
  /** Open with this template already selected (the gallery's USE lands here). */
  preselect?: string;
  onInsert: (t: Template, values: FieldValues) => void;
  onSend: (t: Template, values: FieldValues) => void;
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
  const searchRef = useRef<HTMLInputElement>(null);
  const titleId = "template-picker-title";

  useEffect(() => {
    let alive = true;
    api
      .templates()
      .then((r) => {
        if (!alive) return;
        setTemplates(r.templates);
        const pre = preselect ? r.templates.find((t) => t.id === preselect) : undefined;
        if (pre) setValues(seed(pre));
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
  const preview = selected ? renderTemplate(selected, values) : "";

  const gallery = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!onOpenGallery) return; // no router to hand off to: the anchor navigates
    e.preventDefault();
    onOpenGallery("/templates");
  };

  const select = (t: Template) => {
    if (t.id === selectedId) {
      setSelectedId(null);
      return;
    }
    setSelectedId(t.id);
    setValues(seed(t));
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
                            {f.required ? " · required" : f.default ? ` · default “${f.default}”` : ""}
                          </span>
                          <input
                            className={styles.input}
                            value={values[f.name] ?? ""}
                            onChange={(e) =>
                              setValues((v) => ({ ...v, [f.name]: e.target.value }))
                            }
                            aria-label={f.label}
                            autoComplete="off"
                          />
                        </label>
                      ))}
                      <pre className={styles.preview} aria-label="What will be sent">
                        {preview}
                      </pre>
                      <div className={styles.acts}>
                        <button
                          type="button"
                          className={`${styles.act} ${styles.primary}`}
                          disabled={missing.length > 0}
                          title={
                            missing.length
                              ? `Fill ${missing.length === 1 ? "the required field" : "the required fields"} first`
                              : "Send now, as one message"
                          }
                          aria-label={`Send ${t.name}`}
                          onClick={() => onSend(selected, values)}
                        >
                          <Send size={12} aria-hidden="true" />
                          Send
                        </button>
                        <button
                          type="button"
                          className={styles.act}
                          aria-label={`Insert ${t.name} into composer`}
                          onClick={() => onInsert(selected, values)}
                        >
                          Insert into composer
                        </button>
                      </div>
                      <p className={styles.note}>
                        Sent as one message · recorded in sent history like any other
                      </p>
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
