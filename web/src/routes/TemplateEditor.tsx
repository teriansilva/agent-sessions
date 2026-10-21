import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
} from "react";
import { useBlocker, useLocation, useNavigate, useParams } from "react-router-dom";
import { Copy, ImagePlus, Plus, Trash2, X } from "lucide-react";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { UploadImage } from "../components/templates/UploadImage";
import { api, ApiError } from "../lib/api";
import {
  assembleMessage,
  previewValues,
  splitLibrary,
  substituteFields,
  unknownTokens,
  type LibraryValues,
  type SecretLibrary,
} from "../lib/templateMessage";
import { useIsMobile } from "../lib/useIsMobile";
import type {
  Template,
  TemplateField,
  TemplateImage,
  TemplateInput,
  TemplateLimits,
} from "../types/api";
import styles from "./Templates.module.css";
import { cp, errMessage, FIELD_NAME_RE, formProblems, TAG_RE } from "./templatesLib";

/** The template editor (#905 P2) — `/templates/new` and `/templates/:id`.
 *
 *  The right-hand column is **what the agent receives**: the body with each field's default
 *  substituted (a field with no default keeps its `{{token}}` so the slot stays visible) and
 *  the image paths appended, assembled by the same helper `Compose.send()` uses. It is the
 *  exact text of the paste, never a summary.
 *
 *  Save is fenced by the record's `updated_at` (the server's `expected_updated_at`): a 409
 *  means another tab saved first, and the editor offers the two honest choices — reload
 *  theirs (dropping this draft) or overwrite (save again against the current revision).
 *  There is no merge. Leaving with unsaved edits asks first — through the router's blocker, so
 *  Back/Forward, the topbar gears and every other in-app transition are covered, not only this
 *  page's Cancel (Hermes on #907) — and a reload/close is covered by `beforeunload`. An upload
 *  in flight fences Save and leaving too, and each finished upload is committed to the form
 *  as it lands, so a slow or partially failed multi-file pick never loses an image that did
 *  arrive. Every rule the form checks client-side is re-checked by the server; the server's
 *  422 detail is what the error line shows.
 *
 *  A field's SOURCE (#1090) is this template or the variables library. A library field owns no
 *  default — its cell shows the library's value, read-only, or says the variable is missing —
 *  and the preview substitutes the library value, so it still shows exactly what a send pastes.
 *  The library is read once on open; if it cannot be read the editor still works and says so. */

type Form = TemplateInput;

const EMPTY: Form = { name: "", description: "", tags: [], body: "", fields: [], images: [] };

const FALLBACK_LIMITS: TemplateLimits = {
  templates_max: 200,
  name_max: 120,
  description_max: 300,
  tags_max: 8,
  body_max: 100_000,
  fields_max: 12,
  label_max: 60,
  default_max: 500,
  images_max: 8,
  image_suffixes: [".gif", ".jpeg", ".jpg", ".png", ".webp"],
};

function fromRecord(t: Template): Form {
  return {
    name: t.name,
    description: t.description,
    tags: [...t.tags],
    body: t.body,
    fields: t.fields.map((f) => ({ ...f })),
    images: t.images.map((i) => ({ ...i })),
  };
}

const serialize = (f: Form) => JSON.stringify(f);

const NO_LIBRARY: LibraryValues = {};

export default function TemplateEditor() {
  const { id } = useParams<{ id: string }>();
  // One editor instance per route identity (Hermes on #907, round 2): React Router reuses the
  // element across `/templates/a` → `/templates/b`, and state, refs and in-flight loads must
  // belong to exactly one id — an older load must never land in a newer editor, and a stale
  // leave-bypass must never survive a Duplicate. Keying on the id remounts, which is the
  // cheapest correct answer; the load epoch below covers the same instance's own overlap.
  return <TemplateEditorFor key={id ?? "new"} />;
}

function TemplateEditorFor() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const isMobile = useIsMobile();
  const isNew = !id;
  // #905 P3: "Save as template" from the composer or its history lands here with the body and
  // the image paths prefilled (router state, read once). The snapshot stays EMPTY, so the form
  // is dirty from the start — Save enables as soon as it has a name.
  // Captured on the first render only (a lazy initializer, not a ref: refs must not be read
  // during render), so clearing the entry's state below cannot take the payload away again.
  const [prefill] = useState(() =>
    isNew
      ? (
          location.state as {
            prefill?: {
              body: string;
              images: TemplateImage[];
              // A suggestion (#1090 Phase 3) also carries these; "Save as template" does not.
              name?: string;
              description?: string;
              fields?: TemplateField[];
            };
          } | null
        )?.prefill
      : undefined,
  );
  // Consumed ONCE: the history entry's state is replaced the moment the form has taken it, so
  // a reload — or Back after the save and Forward — lands on an empty new-template editor
  // instead of resurrecting the same payload as a fresh dirty template (Hermes on #908). Same
  // pathname, so the router blocker below lets it through.
  useEffect(() => {
    if (prefill) navigate(location.pathname, { replace: true, state: null });
  }, [prefill, navigate, location.pathname]);

  const [form, setForm] = useState<Form>(() =>
    prefill
      ? {
          ...EMPTY,
          name: prefill.name ?? "",
          description: prefill.description ?? "",
          body: prefill.body,
          fields: prefill.fields ?? [],
          images: prefill.images,
        }
      : EMPTY,
  );
  // What the form holds RIGHT NOW, for code that runs after an await (the save fence below).
  const formRef = useRef(form);
  // The variables library (#1090) — `null` while loading; a failed read is `{}` + a note.
  const [library, setLibrary] = useState<LibraryValues | null>(null);
  const [secretLib, setSecretLib] = useState<SecretLibrary>({});
  const [libraryError, setLibraryError] = useState(false);
  useEffect(() => {
    let alive = true;
    api
      .templateVariables()
      .then((r) => {
        if (!alive) return;
        const lib = splitLibrary(r.variables);
        setLibrary(lib.text);
        setSecretLib(lib.secrets);
      })
      .catch(() => {
        if (!alive) return;
        setLibrary(NO_LIBRARY);
        setLibraryError(true);
      });
    return () => {
      alive = false;
    };
  }, []);
  useEffect(() => {
    formRef.current = form;
  }, [form]);
  // Load epoch: a response from an earlier `load()` is discarded, whatever order they resolve in.
  const loadGenRef = useRef(0);
  const [snapshot, setSnapshot] = useState(serialize(EMPTY));
  const [loaded, setLoaded] = useState<Template | null>(null);
  const [limits, setLimits] = useState<TemplateLimits>(FALLBACK_LIMITS);
  const [state, setState] = useState<"loading" | "ready" | "missing" | "error">(
    isNew ? "ready" : "loading",
  );
  const [error, setError] = useState("");
  const [note, setNote] = useState(
    prefill
      ? prefill.name
        ? "Prefilled from a suggestion — check it, then save."
        : "Prefilled from a sent message — give it a name and save."
      : "",
  );
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [tagDraft, setTagDraft] = useState("");
  const [previewOpen, setPreviewOpen] = useState(true);
  // Set right before an intentional navigation (save, delete, duplicate) so the blocker below
  // lets it through even though `dirty` has not re-rendered to false yet.
  const allowLeaveRef = useRef(false);
  const [deleting, setDeleting] = useState<{ returnTo: HTMLElement | null } | null>(null);
  const [conflict, setConflict] = useState<Template | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const dirty = serialize(form) !== snapshot;
  // Text typed into "Add a tag" and not yet committed is unsaved work too: the blur / Enter
  // that commits it never fires on browser Back or a reload, so those must ask (Hermes on #907,
  // addendum). `dirty` stays what Save keys on — Save's own click blurs the input first, which
  // commits the tag before the save reads the form.
  const unsaved = dirty || tagDraft.trim() !== "";

  // Router-level guard: every in-app transition away from a dirty (or mid-upload) editor —
  // Back/Forward, a topbar gear, a sidebar row, this page's own Cancel — is held until the
  // operator answers. The dialog below drives `blocker.reset()` / `blocker.proceed()`.
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      !allowLeaveRef.current &&
      (unsaved || uploading || saving) &&
      currentLocation.pathname !== nextLocation.pathname,
  );
  // A mutation's continuation belongs to THIS mounted editor: once the operator has left, a
  // late save/duplicate/delete response must not navigate on their behalf (Hermes on #907,
  // round 3). `saving` above also makes leaving mid-mutation a question, not a silent exit.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const load = useCallback(() => {
    const gen = ++loadGenRef.current;
    Promise.resolve()
      .then(() => api.templates())
      .then((r) => {
        if (gen !== loadGenRef.current) return; // superseded by a newer load
        setLimits(r.limits);
        if (isNew) return;
        const rec = r.templates.find((t) => t.id === id);
        if (!rec) {
          setState("missing");
          return;
        }
        const f = fromRecord(rec);
        setForm(f);
        setSnapshot(serialize(f));
        setLoaded(rec);
        setState("ready");
      })
      .catch((e: unknown) => {
        if (gen !== loadGenRef.current) return;
        // Limits are a courtesy; a new template can be edited without them. An existing one
        // cannot be shown without its record.
        if (isNew) return;
        setError(errMessage(e, "Could not load the template"));
        setState("error");
      });
  }, [id, isNew]);
  useEffect(() => {
    load();
  }, [load]);

  // A reload / tab close: the browser asks. In-app navigation is the router blocker's above —
  // this effect covers the one exit the router cannot see.
  useEffect(() => {
    // An upload in flight counts too: its bytes are being stored, and leaving now would orphan
    // them before the form ever committed the reference (Hermes on #907, round 2). So does a
    // mutation out on the wire: a clean template's Duplicate or confirmed Delete sets `saving`
    // with `dirty` false, and a reload then would land the POST/DELETE with its outcome shown
    // to nobody — the same question the router blocker already asks (Hermes on #907, round 4).
    if (!unsaved && !uploading && !saving) return;
    const onBefore = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", onBefore);
    return () => window.removeEventListener("beforeunload", onBefore);
  }, [unsaved, uploading, saving]);

  const patch = (next: Partial<Form>) => setForm((f) => ({ ...f, ...next }));

  const problems = useMemo(() => formProblems(form, limits), [form, limits]);
  const unknown = useMemo(() => unknownTokens(form.body, form.fields), [form.body, form.fields]);
  // Substitute FIRST, trim after — the order `assembleMessage` uses — so a default that carries
  // whitespace previews exactly as it pastes (Hermes on #907, round 3). The <pre> below renders
  // `previewText` plus one space before each path, which is `assembleMessage`'s join, so its
  // textContent is `previewFull` by construction; the unit test pins that identity.
  const previewText = useMemo(
    () =>
      substituteFields(
        form.body,
        form.fields,
        previewValues(form.fields, library ?? NO_LIBRARY),
      ).trim(),
    [form.body, form.fields, library],
  );
  const previewFull = useMemo(
    () => assembleMessage(previewText, form.images.map((i) => i.path)),
    [previewText, form.images],
  );

  const addTag = (raw: string) => {
    const tag = raw.trim().toLowerCase();
    if (!tag) return;
    if (!TAG_RE.test(tag)) {
      setError("a tag is lowercase letters, digits, '-' or '_' (max 24 characters)");
      return;
    }
    if (form.tags.includes(tag) || form.tags.length >= limits.tags_max) {
      setTagDraft("");
      return;
    }
    setError("");
    patch({ tags: [...form.tags, tag] });
    setTagDraft("");
  };
  const onTagKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addTag(tagDraft);
    } else if (e.key === "Backspace" && !tagDraft && form.tags.length) {
      patch({ tags: form.tags.slice(0, -1) });
    }
  };

  const setField = (i: number, next: Partial<TemplateField>) =>
    patch({ fields: form.fields.map((f, j) => (j === i ? { ...f, ...next } : f)) });
  const addField = () =>
    patch({
      fields: [
        ...form.fields,
        { name: "", label: "", default: "", required: false, source: "template", kind: "text" },
      ],
    });
  const removeField = (i: number) => patch({ fields: form.fields.filter((_, j) => j !== i) });

  const pickImages = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    if (fileRef.current) fileRef.current.value = "";
    if (!files.length) return;
    setUploading(true);
    setError("");
    // Each upload is committed to the form the moment it lands — never batched at the end — so
    // a failure on the third file keeps the first two, and Save (fenced while `uploading`)
    // can never persist a template without an image that has already been stored.
    let added = 0;
    let room = limits.images_max - form.images.length;
    try {
      for (const file of files) {
        if (room <= 0) break;
        // The batch belongs to THIS mounted editor. Once the operator has chosen "Discard and
        // leave" mid-batch, the files still queued must not be stored on their behalf, and a
        // result landing after the exit has no form to commit to — it would only be one more
        // orphan (Hermes on #907, round 4). Checked before every start and before every
        // commit; the one request already on the wire is the most that can be orphaned, and
        // aborting it client-side would not un-store what the server has already written.
        if (!mountedRef.current) break;
        try {
          const up = await api.upload(file);
          if (!mountedRef.current) break;
          const img: TemplateImage = { name: up.name, path: up.path };
          setForm((f) => ({ ...f, images: [...f.images, img] }));
          added += 1;
          room -= 1;
        } catch (err) {
          if (!mountedRef.current) break;
          const kept =
            added > 0 ? ` — ${added} ${added === 1 ? "image" : "images"} before it added` : "";
          setError(`${file.name}: ${errMessage(err, "upload failed")}${kept}`);
          break;
        }
      }
    } finally {
      setUploading(false);
    }
  };
  const removeImage = (i: number) => patch({ images: form.images.filter((_, j) => j !== i) });

  const commit = async (expectedUpdatedAt?: number) => {
    // What is being saved is pinned HERE. The form is frozen (`<fieldset disabled>`) while the
    // request is out, and this fence is the second lock: if anything did change the form
    // meanwhile (an upload landing, a programmatic edit), the save is still a save — snapshot
    // moves to what was sent — but the editor stays, with the newer edits still dirty, instead
    // of navigating away over them (Hermes on #907, round 2).
    const submitted = form;
    const submittedKey = serialize(submitted);
    setSaving(true);
    setError("");
    try {
      const rec =
        isNew || !loaded
          ? await api.createTemplate(submitted)
          : await api.updateTemplate(loaded.id, submitted, expectedUpdatedAt ?? loaded.updated_at);
      if (!mountedRef.current) return; // the operator left; the save landed, nothing else to do
      setSnapshot(submittedKey);
      setConflict(null);
      if (!isNew && loaded) setLoaded(rec);
      if (serialize(formRef.current) !== submittedKey) {
        setNote("Saved — but you edited more while it was saving; those edits are still unsaved.");
        return;
      }
      allowLeaveRef.current = true;
      navigate("/templates", { state: { note: `Saved “${rec.name}”` } });
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const cur = (e.record as { current?: Template } | undefined)?.current;
        if (cur) {
          setConflict(cur);
          return;
        }
      }
      setError(errMessage(e, "Could not save the template"));
    } finally {
      setSaving(false);
    }
  };

  // Cancel is an ordinary navigation: the router blocker asks when there is something to lose.
  const leave = () => navigate("/templates");

  const duplicate = async () => {
    setSaving(true);
    setError("");
    try {
      const rec = await api.createTemplate({
        ...form,
        name: `${form.name} (copy)`.slice(0, limits.name_max),
      });
      if (!mountedRef.current) return; // left meanwhile: the copy exists, but this route is not ours
      setSnapshot(serialize(form));
      allowLeaveRef.current = true;
      navigate(`/templates/${encodeURIComponent(rec.id)}`);
    } catch (e) {
      setError(errMessage(e, "Could not duplicate the template"));
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!loaded) return;
    setSaving(true);
    setError("");
    try {
      await api.deleteTemplate(loaded.id, loaded.updated_at);
      if (!mountedRef.current) return;
      setSnapshot(serialize(form)); // nothing left to keep
      allowLeaveRef.current = true;
      navigate("/templates", { state: { note: `Deleted “${loaded.name}”` } });
    } catch (e) {
      setDeleting(null);
      if (e instanceof ApiError && e.status === 409) {
        const cur = (e.record as { current?: Template } | undefined)?.current;
        if (cur) setConflict(cur);
        else setError("The template changed elsewhere — reload it before deleting.");
      } else {
        setError(errMessage(e, "Could not delete the template"));
      }
    } finally {
      setSaving(false);
    }
  };

  const reloadTheirs = () => {
    if (!conflict) return;
    const f = fromRecord(conflict);
    setForm(f);
    setSnapshot(serialize(f));
    setLoaded(conflict);
    setConflict(null);
    setNote("Reloaded the version saved elsewhere; your edits were dropped.");
  };

  if (state === "loading") return <p className={styles.state}>Loading…</p>;
  if (state === "missing")
    return (
      <div className={styles.page}>
        <p className={styles.state}>No such template</p>
        <div className={styles.foot}>
          <button type="button" className={styles.ghost} onClick={leave}>
            Back to templates
          </button>
        </div>
      </div>
    );
  if (state === "error")
    return (
      <div className={styles.page}>
        <p className={styles.err} role="alert">
          {error}
        </p>
        <div className={styles.foot}>
          <button type="button" className={styles.ghost} onClick={() => load()}>
            Retry
          </button>
          <button type="button" className={styles.ghost} onClick={leave}>
            Back to templates
          </button>
        </div>
      </div>
    );

  // Fenced while an upload is in flight: a Save that raced it persisted a template without the
  // image that then landed (Hermes on #907).
  const canSave = problems.length === 0 && !saving && !uploading && (isNew || dirty);
  const showPreview = !isMobile || previewOpen;

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.headLeft}>
          <h1 className={styles.h1}>Template</h1>
          <span className={styles.sl} aria-hidden="true">
            //
          </span>
          <span className={styles.meta}>{isNew ? "new" : loaded?.name}</span>
          {dirty && (
            <>
              <span className={styles.sl} aria-hidden="true">
                //
              </span>
              <span className={styles.meta} style={{ color: "var(--status-draft)" }}>
                unsaved
              </span>
            </>
          )}
        </div>
      </header>

      {note && <p className={styles.note}>{note}</p>}
      {error && (
        <p className={styles.err} role="alert">
          {error}
        </p>
      )}

      <div className={styles.ed}>
        {/* Frozen while a save is out: no edit can slip in between submit and the success path. */}
        <fieldset className={styles.form} disabled={saving}>
          <label className={styles.lbl} htmlFor="tpl-name">
            <span>Name</span>
            <span className={cp(form.name) > limits.name_max ? styles.over : undefined}>
              {cp(form.name)} / {limits.name_max}
            </span>
          </label>
          <input
            id="tpl-name"
            className={styles.input}
            value={form.name}
            onChange={(e) => patch({ name: e.target.value })}
            placeholder="PR review checklist"
            autoComplete="off"
          />

          <label className={styles.lbl} htmlFor="tpl-desc">
            <span>Description · shown on the card</span>
            <span
              className={cp(form.description) > limits.description_max ? styles.over : undefined}
            >
              {cp(form.description)} / {limits.description_max}
            </span>
          </label>
          <input
            id="tpl-desc"
            className={styles.input}
            value={form.description}
            onChange={(e) => patch({ description: e.target.value })}
            placeholder="One line the gallery shows under the name"
            autoComplete="off"
          />

          <div className={styles.lbl} id="tpl-tags-label">
            <span>Tags</span>
            <span>
              {form.tags.length} / {limits.tags_max}
            </span>
          </div>
          <div className={styles.tagRow} role="group" aria-labelledby="tpl-tags-label">
            {form.tags.map((g) => (
              <span key={g} className={styles.tagChip}>
                {g}
                <button
                  type="button"
                  aria-label={`Remove tag ${g}`}
                  onClick={() => patch({ tags: form.tags.filter((x) => x !== g) })}
                >
                  ×
                </button>
              </span>
            ))}
            <input
              value={tagDraft}
              onChange={(e) => setTagDraft(e.target.value)}
              onKeyDown={onTagKey}
              onBlur={() => addTag(tagDraft)}
              placeholder={form.tags.length ? "add tag…" : "review, design, ops…"}
              aria-label="Add a tag"
              autoComplete="off"
            />
          </div>

          <label className={styles.lbl} htmlFor="tpl-body">
            <span>
              Instructions · <span className={styles.tok}>{"{{field}}"}</span> marks a slot you
              fill when you use it
            </span>
            <span className={cp(form.body) > limits.body_max ? styles.over : undefined}>
              {cp(form.body)} / {limits.body_max}
            </span>
          </label>
          <textarea
            id="tpl-body"
            className={styles.area}
            value={form.body}
            onChange={(e) => patch({ body: e.target.value })}
            placeholder={"Review PR {{pr_url}} against our checklist…"}
            spellCheck={false}
          />

          <div className={styles.lbl} id="tpl-fields-label">
            <span>Fields · filled at send time, in this order</span>
            <span>
              {form.fields.length} / {limits.fields_max}
            </span>
          </div>
          {form.fields.length > 0 && (
            <table className={styles.fields} aria-labelledby="tpl-fields-label">
              <thead>
                <tr>
                  <th scope="col">name</th>
                  <th scope="col">source</th>
                  <th scope="col">kind</th>
                  <th scope="col">label</th>
                  <th scope="col">default</th>
                  <th scope="col">required</th>
                  <th scope="col">
                    <span className="sr-only">remove</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {form.fields.map((f, i) => {
                  const bad = f.name !== "" && !FIELD_NAME_RE.test(f.name);
                  return (
                    <tr key={i}>
                      <td className={styles.tokCell}>
                        <input
                          type="text"
                          value={f.name}
                          onChange={(e) =>
                            setField(i, { name: e.target.value.trim().toLowerCase() })
                          }
                          aria-label={`Field ${i + 1} name`}
                          placeholder="pr_url"
                          autoComplete="off"
                        />
                        {bad && <span className={styles.rowErr}>a-z, 0-9, _ · starts with a letter</span>}
                      </td>
                      <td>
                        <select
                          className={styles.sourceSel}
                          value={f.source}
                          onChange={(e) =>
                            // A library field owns no default: switching clears it, so the
                            // server's "one owner for the value" rule never trips on save.
                            setField(
                              i,
                              e.target.value === "library"
                                ? { source: "library", default: "" }
                                : { source: "template" },
                            )
                          }
                          aria-label={`Field ${i + 1} source`}
                        >
                          <option value="template">This template</option>
                          <option value="library">Library</option>
                        </select>
                      </td>
                      <td>
                        <select
                          className={`${styles.sourceSel} ${f.kind === "secret" ? styles.kindSecret : ""}`}
                          value={f.kind}
                          onChange={(e) =>
                            // A secret field owns no default either: it is typed at send time
                            // or stored (encrypted) in the library — never in this template.
                            setField(
                              i,
                              e.target.value === "secret"
                                ? { kind: "secret", default: "" }
                                : { kind: "text" },
                            )
                          }
                          aria-label={`Field ${i + 1} kind`}
                        >
                          <option value="text">Text</option>
                          <option value="secret">🔒 Secret</option>
                        </select>
                      </td>
                      <td>
                        <input
                          type="text"
                          value={f.label}
                          onChange={(e) => setField(i, { label: e.target.value })}
                          aria-label={`Field ${i + 1} label`}
                          placeholder={f.name || "label"}
                          autoComplete="off"
                        />
                      </td>
                      <td>
                        {f.kind === "secret" ? (
                          <SecretCell
                            name={f.name}
                            library={f.source === "library" ? secretLib : null}
                            loading={library === null}
                            failed={libraryError}
                          />
                        ) : f.source === "library" ? (
                          <LibraryCell name={f.name} library={library} failed={libraryError} />
                        ) : (
                          <input
                            type="text"
                            value={f.default}
                            onChange={(e) => setField(i, { default: e.target.value })}
                            aria-label={`Field ${i + 1} default`}
                            placeholder="—"
                            autoComplete="off"
                          />
                        )}
                      </td>
                      <td>
                        {/* The label is the touch target (44×44 on mobile); the box stays 18-24px. */}
                        <label className={styles.checkWrap}>
                          <input
                            type="checkbox"
                            checked={f.required}
                            onChange={(e) => setField(i, { required: e.target.checked })}
                            aria-label={`Field ${i + 1} required`}
                          />
                          {/* The column header is screen-reader-only on a phone, where the row
                              becomes a two-line grid — so the box names itself there. */}
                          <span className={styles.reqHint} aria-hidden="true">
                            req
                          </span>
                        </label>
                      </td>
                      <td>
                        <button
                          type="button"
                          className={styles.iconBtn}
                          aria-label={`Remove field ${f.name || i + 1}`}
                          onClick={() => removeField(i)}
                        >
                          <X size={14} aria-hidden="true" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          <button
            type="button"
            className={styles.addRow}
            onClick={addField}
            disabled={form.fields.length >= limits.fields_max}
          >
            <Plus size={12} aria-hidden="true" />
            Add field
          </button>

          <div className={styles.lbl} id="tpl-images-label">
            <span>Images · sent as file paths after the text · {limits.image_suffixes.join(" ")}</span>
            <span>
              {form.images.length} / {limits.images_max}
            </span>
          </div>
          <div className={styles.imgs} role="group" aria-labelledby="tpl-images-label">
            {form.images.map((im, i) => (
              <div key={im.path} className={styles.im} title={im.path}>
                <UploadImage path={im.path} alt={im.name} />
                <button
                  type="button"
                  className={styles.x}
                  aria-label={`Remove image ${im.name}`}
                  onClick={() => removeImage(i)}
                >
                  <X size={14} aria-hidden="true" />
                </button>
              </div>
            ))}
            <button
              type="button"
              className={styles.imAdd}
              disabled={uploading || saving || form.images.length >= limits.images_max}
              onClick={() => fileRef.current?.click()}
            >
              <ImagePlus size={16} aria-hidden="true" />
              {uploading ? "uploading…" : "Add image"}
            </button>
            <input
              ref={fileRef}
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp"
              multiple
              hidden
              aria-label="Choose images"
              onChange={(e) => void pickImages(e)}
            />
          </div>
        </fieldset>

        <div>
          <div className={styles.lbl}>
            <span>What the agent receives · sample values</span>
            {isMobile && (
              <button
                type="button"
                className={styles.previewToggle}
                onClick={() => setPreviewOpen((v) => !v)}
                aria-expanded={previewOpen}
              >
                {previewOpen ? "▾ hide" : "▸ show"}
              </button>
            )}
          </div>
          {showPreview && (
            <>
              <pre className={styles.preview} aria-label="What the agent receives">
                {previewText}
                {form.images.map((im) => (
                  <span key={im.path}>
                    {previewText || form.images.indexOf(im) > 0 ? " " : ""}
                    <span className={styles.path}>{im.path}</span>
                  </span>
                ))}
              </pre>
              <p className={styles.previewNote}>
                One bracketed paste + Enter — the same path as a typed message · {cp(previewFull)}{" "}
                chars
                {unknown.length > 0 && (
                  <>
                    <br />
                    <span className={styles.previewWarn}>
                      {unknown.map((t) => `{{${t}}}`).join(", ")}{" "}
                      {unknown.length === 1 ? "names no field and is" : "name no field and are"}{" "}
                      sent literally
                    </span>
                  </>
                )}
              </p>
            </>
          )}
        </div>
      </div>

      <div className={styles.foot}>
        <button
          type="button"
          className={styles.cta}
          disabled={!canSave}
          onClick={() => void commit()}
          title={uploading ? "Wait for the upload to finish" : problems[0]}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button type="button" className={styles.ghost} onClick={leave}>
          Cancel
        </button>
        {!isNew && (
          <button
            type="button"
            className={styles.ghost}
            disabled={saving || uploading || problems.length > 0}
            onClick={() => void duplicate()}
          >
            <Copy size={13} aria-hidden="true" />
            Duplicate
          </button>
        )}
        <span className={styles.sp} />
        {!isNew && (
          <button
            type="button"
            className={styles.danger}
            disabled={saving || uploading}
            onClick={(e) => setDeleting({ returnTo: e.currentTarget })}
          >
            <Trash2 size={13} aria-hidden="true" />
            Delete template
          </button>
        )}
      </div>

      {blocker.state === "blocked" && (
        <ConfirmDialog
          tag={uploading ? "Upload in progress" : saving ? "Save in progress" : "Unsaved changes"}
          title={form.name || "New template"}
          cancelLabel="Keep editing"
          confirmLabel="Discard and leave"
          danger
          onCancel={() => blocker.reset()}
          onConfirm={() => blocker.proceed()}
        >
          <p>
            {uploading
              ? "An image is still uploading. Leave and lose it, or stay until it lands."
              : saving
                ? "A save is still in flight. If you leave, it lands quietly and you will not be taken to the result."
                : "Leave the editor and discard the edits, or stay and save."}
          </p>
        </ConfirmDialog>
      )}

      {deleting && loaded && (
        <ConfirmDialog
          tag="Delete template"
          title={loaded.name}
          confirmLabel="Delete"
          danger
          busy={saving}
          onCancel={() => setDeleting(null)}
          onConfirm={() => void confirmDelete()}
          returnFocusTo={deleting.returnTo}
        >
          <p>
            Removes the template from the gallery.
            {loaded.images.length > 0 &&
              ` Its ${loaded.images.length === 1 ? "image stays" : `${loaded.images.length} images stay`} in the uploads folder.`}{" "}
            This cannot be undone.
          </p>
        </ConfirmDialog>
      )}

      {conflict && (
        <ConfirmDialog
          tag="Changed elsewhere"
          title={conflict.name}
          cancelLabel="Reload theirs"
          confirmLabel="Overwrite"
          busy={saving}
          onCancel={reloadTheirs}
          onConfirm={() => void commit(conflict.updated_at)}
        >
          <p>
            This template was saved from another tab or device since you opened it. Reload
            their version (your edits are dropped), or overwrite it with yours.
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}

/** A library field's value cell: the variable's value, read-only (it is edited under Variables,
 *  where the change reaches every template that uses it), or why there is none. */
function LibraryCell({
  name,
  library,
  failed,
}: {
  name: string;
  library: LibraryValues | null;
  failed: boolean;
}) {
  if (library === null) return <span className={styles.libVal}>loading…</span>;
  if (failed) return <span className={styles.libMissing}>library not loaded</span>;
  if (!name || !Object.hasOwn(library, name)) {
    return (
      <span className={styles.libMissing}>
        {name ? "missing library variable" : "name it after a variable"}
      </span>
    );
  }
  return (
    <span className={styles.libVal} title={library[name]}>
      {library[name]}
    </span>
  );
}

/** A secret field's value cell (#1090 Phase 2). There is never a value to show: a library secret
 *  says whether one is stored (and still decrypts); a typed-once one says it is asked for at send
 *  time and not kept. */
function SecretCell({
  name,
  library,
  loading,
  failed,
}: {
  name: string;
  library: SecretLibrary | null;
  loading: boolean;
  failed: boolean;
}) {
  if (library === null) return <span className={styles.libVal}>typed at send time · not stored</span>;
  if (loading) return <span className={styles.libVal}>loading…</span>;
  if (failed) return <span className={styles.libMissing}>library not loaded</span>;
  const state = name && Object.hasOwn(library, name) ? library[name] : undefined;
  if (state === "ok") return <span className={styles.libVal}>•••••••• stored</span>;
  if (state === "reentry") return <span className={styles.libMissing}>needs re-entry</span>;
  return (
    <span className={styles.libMissing}>
      {name ? "missing secret variable" : "name it after a secret variable"}
    </span>
  );
}
