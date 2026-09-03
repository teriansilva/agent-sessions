import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Copy, Pencil, Plus, Search, Trash2 } from "lucide-react";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { UploadImage } from "../components/templates/UploadImage";
import { api, ApiError } from "../lib/api";
import type { Template, TemplateLimits } from "../types/api";
import styles from "./Templates.module.css";
import { errMessage, metaLine } from "./templatesLib";

/** TEMPLATES — the gallery of instruction templates (#905 P2). Its own top-level route
 *  beside Pulse / Overview / Settings, not a settings sub-panel: cards, search, tag chips,
 *  NEW TEMPLATE, and per-card Edit / Duplicate / Delete. The list is the server's, most
 *  recently used first; search and the tag filter are client-side over that list (a library
 *  of at most 200 records). The chips count over the UNFILTERED set, as Pulse's do, so a tag
 *  never disappears because of the current selection.
 *
 *  Delete goes through a real confirm (not `window.confirm`): a template is authored text,
 *  not cheap metadata. It sends the card's `updated_at` as the fence, so a delete against a
 *  record another tab just edited is a 409 the gallery folds back in by reloading. */

function matches(t: Template, q: string): boolean {
  if (!q) return true;
  const hay = `${t.name}\n${t.description}\n${t.tags.join(" ")}\n${t.body}`.toLowerCase();
  return hay.includes(q);
}

export default function Templates() {
  const navigate = useNavigate();
  const location = useLocation();
  const [templates, setTemplates] = useState<Template[] | null>(null);
  const [limits, setLimits] = useState<TemplateLimits | null>(null);
  const [error, setError] = useState("");
  // The editor navigates here with `state.note` after a save or delete (one-shot, not a URL).
  const [note, setNote] = useState(
    () => (location.state as { note?: string } | null)?.note ?? "",
  );
  const [query, setQuery] = useState("");
  const [tag, setTag] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<{
    t: Template;
    returnTo: HTMLElement | null;
  } | null>(null);

  // Resolves to whether the refresh succeeded: a caller that just deleted or hit a conflict
  // must not claim "reloaded" over a failed reload (Hermes on #907, round 3).
  const load = useCallback(
    (): Promise<boolean> =>
      Promise.resolve()
        .then(() => api.templates())
        .then((r) => {
          setTemplates(r.templates);
          setLimits(r.limits);
          setError("");
          return true;
        })
        .catch((e: unknown) => {
          setError(errMessage(e, "Could not load templates"));
          return false;
        }),
    [],
  );
  // A late duplicate response must not navigate once the operator has left this page.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const tagCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const t of templates ?? []) for (const g of t.tags) m.set(g, (m.get(g) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [templates]);
  // A tag filter that no longer exists (its last template was deleted) is dropped.
  const effTag = tag && tagCounts.some(([g]) => g === tag) ? tag : null;

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (templates ?? []).filter((t) => (!effTag || t.tags.includes(effTag)) && matches(t, q));
  }, [templates, effTag, query]);

  const duplicate = async (t: Template) => {
    setBusy(true);
    setError("");
    try {
      const max = limits?.name_max ?? 120;
      const rec = await api.createTemplate({
        name: `${t.name} (copy)`.slice(0, max),
        description: t.description,
        tags: t.tags,
        body: t.body,
        fields: t.fields,
        images: t.images,
      });
      if (!mountedRef.current) return;
      navigate(`/templates/${encodeURIComponent(rec.id)}`);
    } catch (e) {
      setError(errMessage(e, "Could not duplicate the template"));
    } finally {
      setBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    const { t } = pendingDelete;
    setBusy(true);
    setError("");
    try {
      await api.deleteTemplate(t.id, t.updated_at);
      setPendingDelete(null);
      // The row is gone server-side: drop it locally FIRST, so a failed refresh can never leave
      // a deleted card actionable; then refresh, and say what actually happened.
      setTemplates((ts) => (ts ? ts.filter((x) => x.id !== t.id) : ts));
      const ok = await load();
      setNote(
        ok
          ? `Deleted “${t.name}”. Its images stay in the uploads folder.`
          : `Deleted “${t.name}”, but the gallery could not be refreshed — the list shown is local.`,
      );
    } catch (e) {
      setPendingDelete(null);
      // Reload FIRST, then state the outcome — including a refresh that failed, which must
      // never read as "reloaded".
      if (e instanceof ApiError && e.status === 409) {
        const ok = await load();
        setError(
          ok
            ? `“${t.name}” changed elsewhere since this page loaded — reloaded, nothing deleted.`
            : `“${t.name}” changed elsewhere since this page loaded, and reloading the gallery failed — nothing deleted; try again.`,
        );
      } else if (e instanceof ApiError && e.status === 404) {
        // A 404 is the server saying the row is already gone — the same fact a successful
        // delete establishes, so the same rule: drop it locally FIRST, then refresh
        // best-effort. A failed refresh must never leave a card the server does not have
        // openable or deletable (Hermes on #907, round 4).
        setTemplates((ts) => (ts ? ts.filter((x) => x.id !== t.id) : ts));
        const ok = await load();
        setError(
          ok
            ? `“${t.name}” was already deleted elsewhere — reloaded.`
            : `“${t.name}” was already deleted elsewhere, and reloading the gallery failed — the list shown is local.`,
        );
      } else {
        setError(errMessage(e, "Could not delete the template"));
      }
    } finally {
      setBusy(false);
    }
  };

  const total = templates?.length ?? 0;

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.headLeft}>
          <h1 className={styles.h1}>Templates</h1>
          <span className={styles.sl} aria-hidden="true">
            //
          </span>
          <span className={styles.meta}>
            {templates === null ? (error ? "not loaded" : "loading") : `${total} saved`}
          </span>
          {total > 0 && (
            <>
              <span className={styles.sl} aria-hidden="true">
                //
              </span>
              <span className={styles.meta}>sorted by last use</span>
            </>
          )}
        </div>
        <div className={styles.headRight}>
          <Link to="/templates/new" className={styles.cta}>
            <Plus size={14} aria-hidden="true" />
            New template
          </Link>
        </div>
      </header>

      {note && <p className={styles.note}>{note}</p>}
      {error && (
        <p className={styles.err} role="alert">
          {error}
          {templates === null && (
            // The first load failed: there is no list to show, so say so (the header reads
            // "not loaded", never "loading") and offer the retry (Hermes on #907, addendum).
            <button
              type="button"
              className={styles.retry}
              onClick={() => {
                setError("");
                void load();
              }}
            >
              Retry
            </button>
          )}
        </p>
      )}

      {total > 0 && (
        <div className={styles.tools}>
          <label className={styles.search}>
            <Search size={14} aria-hidden="true" />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search name, tag, text…"
              aria-label="Search templates"
            />
          </label>
          {tagCounts.length > 0 && (
            <div className={styles.chips} role="group" aria-label="Filter by tag">
              <button
                type="button"
                className={`${styles.chip} ${effTag === null ? styles.chipOn : ""}`}
                aria-pressed={effTag === null}
                onClick={() => setTag(null)}
              >
                All <span className={styles.chipN}>{total}</span>
              </button>
              {tagCounts.map(([g, n]) => (
                <button
                  key={g}
                  type="button"
                  className={`${styles.chip} ${effTag === g ? styles.chipOn : ""}`}
                  aria-pressed={effTag === g}
                  onClick={() => setTag(effTag === g ? null : g)}
                >
                  {g} <span className={styles.chipN}>{n}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {templates !== null && total === 0 && (
        <div className={styles.empty}>
          <span className={styles.emptyGlyph} aria-hidden="true">
            &gt;_
          </span>
          <p className={styles.emptyTitle}>No templates yet</p>
          <p className={styles.emptyHint}>
            A template is an instruction you send more than once — the text, the reference
            images, and the <code>{"{{fields}}"}</code> that change each time. Start one here, or
            save a message you already sent from the composer&apos;s history.
          </p>
          <div className={styles.emptyActs}>
            <Link to="/templates/new" className={styles.cta}>
              <Plus size={14} aria-hidden="true" />
              New template
            </Link>
          </div>
        </div>
      )}

      {total > 0 && visible.length === 0 && (
        <p className={styles.state}>No template matches</p>
      )}

      {visible.length > 0 && (
        <ul className={styles.cards} aria-label="Templates">
          {visible.map((t) => (
            <li key={t.id} className={styles.card} data-template-id={t.id}>
              <div className={styles.thumb}>
                {t.images[0] ? (
                  <UploadImage
                    path={t.images[0].path}
                    alt=""
                    fallback={
                      <span className={styles.glyph} aria-hidden="true">
                        &gt;_
                      </span>
                    }
                  />
                ) : (
                  <span className={styles.glyph} aria-hidden="true">
                    &gt;_
                  </span>
                )}
                {t.images.length > 0 && (
                  <span className={styles.thumbN}>
                    {t.images.length} img
                  </span>
                )}
              </div>
              <div className={styles.cbody}>
                <h2 className={styles.cname}>
                  <Link to={`/templates/${encodeURIComponent(t.id)}`}>{t.name}</Link>
                </h2>
                {t.description && <p className={styles.cdesc}>{t.description}</p>}
                {t.tags.length > 0 && (
                  <div className={styles.tags}>
                    {t.tags.map((g) => (
                      <span key={g} className={styles.tag}>
                        {g}
                      </span>
                    ))}
                  </div>
                )}
                <p className={styles.cmeta}>{metaLine(t)}</p>
              </div>
              <div className={styles.acts}>
                <Link
                  to={`/templates/${encodeURIComponent(t.id)}`}
                  className={styles.act}
                  aria-label={`Edit ${t.name}`}
                >
                  <Pencil size={13} aria-hidden="true" />
                  Edit
                </Link>
                <button
                  type="button"
                  className={styles.act}
                  aria-label={`Duplicate ${t.name}`}
                  disabled={busy}
                  onClick={() => void duplicate(t)}
                >
                  <Copy size={13} aria-hidden="true" />
                  Duplicate
                </button>
                <button
                  type="button"
                  className={`${styles.act} ${styles.actDanger}`}
                  aria-label={`Delete ${t.name}`}
                  disabled={busy}
                  onClick={(e) => setPendingDelete({ t, returnTo: e.currentTarget })}
                >
                  <Trash2 size={13} aria-hidden="true" />
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {pendingDelete && (
        <ConfirmDialog
          tag="Delete template"
          title={pendingDelete.t.name}
          confirmLabel="Delete"
          danger
          busy={busy}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => void confirmDelete()}
          returnFocusTo={pendingDelete.returnTo}
        >
          <p>
            Removes the template from the gallery.
            {pendingDelete.t.images.length > 0 &&
              ` Its ${pendingDelete.t.images.length} ${
                pendingDelete.t.images.length === 1 ? "image stays" : "images stay"
              } in the uploads folder — a draft or a sent message may still point at ${
                pendingDelete.t.images.length === 1 ? "it" : "them"
              }.`}{" "}
            This cannot be undone.
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}
