import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import { Search, Send, X } from "lucide-react";
import { api } from "../../lib/api";
import {
  hasSecret,
  maskedValues,
  missingLibrary,
  missingRequired,
  renderTemplate,
  seedValues,
  shortSecrets,
  splitLibrary,
  type LibraryValues,
  type SecretLibrary,
} from "../../lib/templateMessage";
import { errMessage } from "../../routes/templatesLib";
import type { Template, TemplateSendResult } from "../../types/api";
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
 *  missing, and it says why.
 *
 *  SECRET fields (#1090 Phase 2) change the send path, not just the inputs. A template with any
 *  secret field is rendered and delivered by the SERVER (`api.sendTemplate`), because the browser
 *  never holds a stored secret: a stored secret shows only as "stored in the library"; a
 *  typed-once one is a password input whose value leaves this dialog in exactly one request and
 *  is never kept. The preview shows `[secret: name]`, and so does the sent history — the masked
 *  text the route answers with. Insert is never offered for such a template (a composer or a
 *  mission brief would have to hold the value), and Send needs a real session id: a fresh
 *  session that has not reconciled yet has none to bind the send to. */

export type FieldValues = Record<string, string>;

const NO_LIBRARY: LibraryValues = {};
const NO_SECRETS: SecretLibrary = {};
const SECRET_MIN_FALLBACK = 8;

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
  sessionId,
  onServerSent,
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
  /** The session a template with SECRET fields is sent into, server-side (#1090 Phase 2).
   *  `null` = a session with no real id yet (an unreconciled launch): such a template cannot be
   *  sent. Absent together with `onServerSent` = no session at all (the mission brief). */
  sessionId?: string | null;
  /** A server-side send landed: the MASKED text, for the composer's sent history. */
  onServerSent?: (t: Template, result: TemplateSendResult) => void;
}) {
  const [templates, setTemplates] = useState<Template[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(preselect ?? null);
  const [values, setValues] = useState<FieldValues>({});
  // `null` until loaded; a failed load is an empty library plus `libraryError`, never a blocker.
  const [library, setLibrary] = useState<LibraryValues | null>(null);
  const [secretLib, setSecretLib] = useState<SecretLibrary>(NO_SECRETS);
  const [secretMin, setSecretMin] = useState(SECRET_MIN_FALLBACK);
  const [libraryError, setLibraryError] = useState(false);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const titleId = "template-picker-title";

  useEffect(() => {
    let alive = true;
    const vars = api
      .templateVariables()
      .then((r) => {
        if (alive && r.limits?.secret_min) setSecretMin(r.limits.secret_min);
        return splitLibrary(r.variables);
      })
      .catch(() => {
        if (alive) setLibraryError(true);
        return { text: NO_LIBRARY, secrets: NO_SECRETS };
      });
    Promise.all([api.templates(), vars])
      .then(([r, lib]) => {
        if (!alive) return;
        setTemplates(r.templates);
        setLibrary(lib.text);
        setSecretLib(lib.secrets);
        const pre = preselect ? r.templates.find((t) => t.id === preselect) : undefined;
        if (pre) setValues(seedValues(pre.fields, lib.text));
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

  // A server-side send in flight pins the dialog (Hermes on #1105): dismissing it and reopening
  // another would let the OLD response close the NEW picker and drop its draft. Every way out —
  // Escape, the backdrop, Close — waits until the send has settled.
  const sendingRef = useRef(false);
  useEffect(() => {
    sendingRef.current = sending;
  }, [sending]);
  const dismiss = () => {
    if (!sendingRef.current) onClose();
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        if (!sendingRef.current) onClose();
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
  const unset = selected ? missingLibrary(selected.fields, library ?? NO_LIBRARY, secretLib) : [];
  const blocked = unset.length > 0;
  const secret = selected ? hasSecret(selected.fields) : false;
  const short = selected && secret ? shortSecrets(selected.fields, values, secretMin) : [];
  // The server refuses a secret with edge whitespace (assembly would trim it out of redaction's
  // reach); say so here rather than after a round trip.
  const padded =
    selected && secret
      ? selected.fields.filter(
          (f) =>
            f.kind === "secret" &&
            f.source !== "library" &&
            (values[f.name] ?? "") !== (values[f.name] ?? "").trim(),
        )
      : [];
  // A secret template goes to the server with the session's REAL id; none ⇒ it cannot be sent.
  const noTarget = secret && !sessionId;
  const preview = selected
    ? renderTemplate(selected, secret ? maskedValues(selected.fields, values) : values)
    : "";

  const sendServerSide = async (t: Template) => {
    if (!sessionId || sending) return;
    sendingRef.current = true; // pinned from this instant, not from the next render
    setSending(true);
    setSendError("");
    // Only what the browser holds: template fields, library-text overrides and typed-once
    // secrets. A stored secret has no value here and is never sent from here.
    const payload: FieldValues = {};
    for (const f of t.fields) {
      if (f.kind === "secret" && f.source === "library") continue;
      payload[f.name] = values[f.name] ?? "";
    }
    try {
      const result = await api.sendTemplate(t.id, sessionId, payload, t.updated_at);
      setValues({}); // typed-once secrets leave the dialog's state with the send
      onServerSent?.(t, result);
    } catch (e) {
      setSendError(errMessage(e, "Could not send the template"));
    } finally {
      setSending(false);
    }
  };

  // While a server-side send is in flight, EVERY way out of or around this dialog waits — not
  // only Escape and Close (Hermes on #1105, round 2): the gallery links, Insert, choosing another
  // template and editing a field would each let the pending response act on a state it did not
  // start from (close a newer picker, clear newer values).
  const gallery = (e: MouseEvent<HTMLAnchorElement>) => {
    if (sendingRef.current) {
      e.preventDefault();
      return;
    }
    if (!onOpenGallery) return; // no router to hand off to: the anchor navigates
    e.preventDefault();
    onOpenGallery("/templates");
  };

  const variablesLink = (e: MouseEvent<HTMLAnchorElement>) => {
    if (sendingRef.current) {
      e.preventDefault();
      return;
    }
    if (!onOpenGallery) return;
    e.preventDefault();
    onOpenGallery("/templates?tab=variables");
  };

  const select = (t: Template) => {
    if (sendingRef.current) return;
    if (t.id === selectedId) {
      setSelectedId(null);
      return;
    }
    setSelectedId(t.id);
    setSendError("");
    setValues(seedValues(t.fields, library ?? NO_LIBRARY));
  };

  return createPortal(
    <div className={styles.backdrop} onMouseDown={dismiss}>
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
            onClick={dismiss}
            disabled={sending}
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
                    disabled={sending}
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
                            {f.kind === "secret" ? " · secret" : ""}
                            {f.source === "library"
                              ? unset.includes(f.name)
                                ? secretLib[f.name] === "reentry"
                                  ? " · needs re-entry"
                                  : " · missing library variable"
                                : " · from library"
                              : f.kind === "secret"
                                ? " · typed now, not stored"
                                : !f.required && f.default
                                  ? ` · default “${f.default}”`
                                  : ""}
                          </span>
                          {f.kind === "secret" && f.source === "library" ? (
                            // The browser has no value to show — only that one is stored.
                            <span className={styles.stored} aria-label={f.label}>
                              {unset.includes(f.name) ? "—" : "•••••••• stored"}
                            </span>
                          ) : f.kind === "secret" ? (
                            <input
                              type="password"
                              className={styles.input}
                              value={values[f.name] ?? ""}
                              readOnly={sending}
                              onChange={(e) =>
                                setValues((v) => ({ ...v, [f.name]: e.target.value }))
                              }
                              aria-label={f.label}
                              autoComplete="off"
                              spellCheck={false}
                            />
                          ) : (
                            /* A textarea, never an <input>: a field value may span lines (a
                               default or a library value both may), and an input silently
                               drops the newlines — `cd repo⏎npm test` would send as
                               `cd reponpm test` (Hermes on #1095). */
                            <textarea
                              className={styles.input}
                              value={values[f.name] ?? ""}
                              rows={Math.min(
                                6,
                                Math.max(1, (values[f.name] ?? "").split("\n").length),
                              )}
                              readOnly={sending}
                              onChange={(e) =>
                                setValues((v) => ({ ...v, [f.name]: e.target.value }))
                              }
                              aria-label={f.label}
                              autoComplete="off"
                              spellCheck={false}
                            />
                          )}
                        </label>
                      ))}
                      <pre className={styles.preview} aria-label="What will be sent">
                        {preview}
                      </pre>
                      {blocked && unset.some((n) => secretLib[n] === "reentry") && (
                        <p className={styles.blocked} role="alert">
                          {unset
                            .filter((n) => secretLib[n] === "reentry")
                            .map((n) => `{{${n}}}`)
                            .join(", ")}{" "}
                          can no longer be decrypted — enter it again under{" "}
                          <a href="/templates?tab=variables" onClick={variablesLink}>
                            Variables
                          </a>
                          . Nothing will be sent until then.
                        </p>
                      )}
                      {blocked && unset.some((n) => secretLib[n] !== "reentry") && (
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
                              {unset
                                .filter((n) => secretLib[n] !== "reentry")
                                .map((n) => `{{${n}}}`)
                                .join(", ")}{" "}
                              — add{" "}
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
                            disabled={
                              missing.length > 0 ||
                              blocked ||
                              short.length > 0 ||
                              padded.length > 0 ||
                              noTarget ||
                              sending
                            }
                            title={
                              blocked
                                ? "A library variable this template uses is missing or unreadable"
                                : noTarget
                                  ? "This session has no id yet — send once it has started"
                                  : missing.length
                                    ? `Fill ${missing.length === 1 ? "the required field" : "the required fields"} first`
                                    : short.length
                                      ? `A secret is at least ${secretMin} characters`
                                      : padded.length
                                        ? "A secret cannot start or end with a space"
                                      : secret
                                        ? "Send now — the server fills in the secrets"
                                        : "Send now, as one message"
                            }
                            aria-label={`Send ${t.name}`}
                            onClick={() =>
                              secret ? void sendServerSide(selected) : onSend?.(selected, values)
                            }
                          >
                            <Send size={12} aria-hidden="true" />
                            {sending ? "Sending…" : "Send"}
                          </button>
                        ) : null}
                        <button
                          type="button"
                          className={styles.act}
                          aria-label={`Insert ${t.name} into ${onSend ? "composer" : "mission brief"}`}
                          disabled={blocked || secret || sending}
                          title={
                            secret
                              ? "A template with secret fields can only be sent — inserting it would put the secret in the text box"
                              : undefined
                          }
                          onClick={() => {
                            if (!sendingRef.current) onInsert(selected, values);
                          }}
                        >
                          {insertLabel}
                        </button>
                      </div>
                      {sendError && (
                        <p className={styles.blocked} role="alert">
                          {sendError}
                        </p>
                      )}
                      {secret && (
                        <p className={styles.note}>
                          {onSend
                            ? noTarget
                              ? "Has secret fields · can be sent once this session has started"
                              : `Has secret fields · the server fills them in · your history keeps [secret: …]${short.length ? ` · a secret is at least ${secretMin} characters` : ""}`
                            : "Has secret fields · it can only be sent into a session, not inserted"}
                        </p>
                      )}
                      {onSend && !secret ? (
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
