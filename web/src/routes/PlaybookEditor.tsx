/** A complete bundle draft, revision-fenced saves, and explicit stale-draft recovery (#1359). */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Link,
  useBlocker,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import { ApiError, api } from "../lib/api";
import { PLAYBOOKS_PATH, playbookEditPath } from "../lib/routes";
import { PlaybookValidation } from "../components/playbooks/playbookValidation";
import { PlaybookDialog } from "../components/playbooks/PlaybookDialog";
import {
  PlaybookStepEditor,
  TextField,
  Choice,
} from "../components/playbooks/PlaybookStepEditor";
import { PlaybookVariables } from "../components/playbooks/PlaybookVariables";
import { PlaybookActor } from "../components/playbooks/PlaybookCard";
import {
  errorText,
  usePlaybookRead,
} from "../components/playbooks/usePlaybooks";
import {
  MANIFEST,
  differences,
  draftFiles,
  flowId,
  flowPath,
  freshId,
  fromDetail,
  newDraft,
  newStep,
  same,
} from "../components/playbooks/playbookDraft";
import type {
  AuthoringSchema,
  Draft,
  Flow,
  Manifest,
} from "../components/playbooks/playbookDraft";
import type { PlaybookDetail, PlaybookWriteResult } from "../types/playbooks";
import buttons from "../components/ui/actionButton.module.css";
import styles from "../components/playbooks/playbookEditor.module.css";
import { PlaybookFlowCanvas } from "../components/playbooks/PlaybookFlowCanvas";
import { useIsMobile } from "../lib/useIsMobile";

export default function PlaybookEditor() {
  const { playbookId } = useParams();
  const schema = usePlaybookRead(
    useCallback(() => api.playbookAuthoring(), []),
  );
  const detail = usePlaybookRead(
    useCallback(
      () => (playbookId ? api.playbook(playbookId) : Promise.resolve(null)),
      [playbookId],
    ),
  );
  return (
    <main className={styles.editor} data-testid="playbook-editor">
      {(schema.error || detail.error) && (
        <div role="alert" className={styles.error}>
          {schema.error || detail.error}
          <button
            className={buttons.ghost}
            onClick={() => {
              void schema.reload();
              void detail.reload();
            }}
          >
            Try again
          </button>
        </div>
      )}
      {!schema.data || (playbookId && detail.data?.id !== playbookId) ? (
        <p role="status">Loading editor…</p>
      ) : playbookId && (!detail.data?.editable || !detail.data.ok) ? (
        <>
          <h1>Playbook cannot be edited here</h1>
          <p>Duplicate a valid bundled or catalog playbook to local first.</p>
          <Link to={PLAYBOOKS_PATH}>All playbooks</Link>
        </>
      ) : (
        <Editor
          key={playbookId ?? "new"}
          original={detail.data ?? undefined}
          schema={schema.data}
        />
      )}
    </main>
  );
}
function Editor({
  original,
  schema,
}: {
  original?: PlaybookDetail;
  schema: AuthoringSchema;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [baseline, setBaseline] = useState<Draft>(() =>
    original ? fromDetail(original) : newDraft(),
  );
  const [draft, setDraft] = useState<Draft>(() => structuredClone(baseline));
  const [revision, setRevision] = useState(original?.revision ?? null);
  const [path, setPath] = useState(
    () =>
      Object.keys(baseline.documents).find((p) => p.startsWith("flows/")) ?? "",
  );
  const [selected, setSelected] = useState("");
  const [view, setView] = useState<"canvas" | "list">("canvas");
  const mobile = useIsMobile();
  const canvas = !mobile && view === "canvas";
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const allowLeave = useRef(false);
  const [error, setError] = useState("");
  const [errorField, setErrorField] = useState("");
  const [notice, setNotice] = useState<string>(() =>
    typeof location.state?.savedNotice === "string"
      ? location.state.savedNotice
      : "",
  );
  const [conflict, setConflict] = useState(false);
  const [compared, setCompared] = useState<PlaybookDetail | null>(null);
  const [remove, setRemove] = useState<{
    title: string;
    apply: () => void;
  } | null>(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const dirty = !same(draft, baseline);
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      !allowLeave.current &&
      (dirty || busyRef.current) &&
      currentLocation.pathname !== nextLocation.pathname,
  );
  useEffect(() => {
    const before = (e: BeforeUnloadEvent) => {
      if (!allowLeave.current && (dirty || busyRef.current)) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", before);
    return () => window.removeEventListener("beforeunload", before);
  }, [dirty]);
  const manifest = draft.documents[MANIFEST] as Manifest;
  const paths = Object.keys(draft.documents).filter(
    (p) => p.startsWith("flows/") && p.endsWith(".toml"),
  );
  const flow = draft.documents[path] as Flow | undefined;
  const selectedStep =
    flow?.steps.find((s) => s.id === selected) ?? flow?.steps[0];
  function edit(next: Draft) {
    setDraft(next);
    setNotice("");
    setError("");
    setErrorField("");
  }
  function showSteps() {
    const heading = window.document.querySelector<HTMLElement>(
      '[aria-label="Flow steps"] h2',
    );
    heading?.focus();
    heading?.scrollIntoView({ block: "start" });
  }
  function selectStep(id: string) {
    setSelected(id);
    if (window.matchMedia("(max-width: 800px)").matches)
      requestAnimationFrame(() => {
        const heading = window.document.querySelector<HTMLElement>(
          '[aria-label="Step inspector"] h2',
        );
        heading?.focus();
        heading?.scrollIntoView({ block: "start" });
      });
  }
  function document(path: string, doc: Draft["documents"][string]) {
    edit({ ...draft, documents: { ...draft.documents, [path]: doc } });
  }
  function identity(key: keyof Manifest["identity"], value: string) {
    document(MANIFEST, {
      ...manifest,
      identity: { ...manifest.identity, [key]: value },
    });
  }
  function report(e: unknown) {
    setError(errorText(e));
    const record =
      e instanceof ApiError
        ? (e.record as { field?: unknown } | undefined)
        : undefined;
    setErrorField(typeof record?.field === "string" ? record.field : "");
    // A create-ID collision has no loaded revision to compare. Keep ordinary save
    // available so the operator can choose a free ID without losing the draft.
    if (original && e instanceof ApiError && e.status === 409) {
      setConflict(true);
      setCompared(null);
    }
  }
  function adopt(pb: PlaybookDetail) {
    const next = fromDetail(pb);
    setDraft(next);
    setBaseline(next);
    setRevision(pb.revision);
    setConflict(false);
    setCompared(null);
    setError("");
    setErrorField("");
    setPath((old) =>
      next.documents[old]
        ? old
        : (Object.keys(next.documents).find((p) => p.startsWith("flows/")) ??
          ""),
    );
  }
  async function save(copy = false) {
    if (busyRef.current || (!copy && conflict && !compared)) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    setErrorField("");
    try {
      const files = draftFiles(draft, baseline);
      const pb = copy
        ? await api.copyPlaybookDraft(files)
        : original
          ? await api.savePlaybook(
              original.id,
              compared?.revision ?? revision!,
              files,
            )
          : await api.createPlaybook(files);
      if (!live.current) return;
      adopt(pb);
      const result: PlaybookWriteResult = pb;
      const warning =
        result.durable === false || result.recovery_durable === false;
      const savedNotice = warning
        ? "Saved, but durable storage or recovery could not be confirmed. Inspect the store before further changes."
        : "Playbook saved. Existing projects keep their pinned revision.";
      setNotice(savedNotice);
      if (copy || !original) {
        allowLeave.current = true;
        navigate(playbookEditPath(pb.id), {
          replace: true,
          state: { savedNotice },
        });
      }
    } catch (e) {
      if (live.current) report(e);
    } finally {
      busyRef.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function current(discard: boolean) {
    if (!original || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      const pb = await api.playbook(original.id);
      if (!live.current) return;
      if (!pb.ok || !pb.editable || !pb.revision)
        throw new Error(
          "The current playbook cannot be edited. Your draft is retained; save it as a new playbook.",
        );
      if (discard) {
        adopt(pb);
        setNotice("Loaded the current saved playbook.");
      } else {
        setCompared(pb);
        setError("");
      }
    } catch (e) {
      if (live.current) report(e);
    } finally {
      busyRef.current = false;
      if (live.current) setBusy(false);
    }
  }
  function showErrorField() {
    const match = errorField.match(/steps\[(\d+)\]/);
    const errorPath = paths.find((p) => error.includes(p));
    if (errorPath) setPath(errorPath);
    if (match) {
      const f = draft.documents[errorPath ?? path] as Flow;
      const step = f?.steps[Number(match[1])];
      if (step) setSelected(step.id);
    }
    requestAnimationFrame(() => {
      const field = [
        ...window.document.querySelectorAll<HTMLElement>("[data-field]"),
      ].find((el) => errorField.endsWith(el.dataset.field ?? "!"));
      if (field) {
        field.closest("details")?.setAttribute("open", "");
        field.focus();
        field.scrollIntoView({ block: "center" });
      }
    });
  }
  return (
    <>
      <div className={styles.toolbar}>
        <div>
          <Link to={PLAYBOOKS_PATH}>← All playbooks</Link>
          <h1>{original ? "Edit playbook" : "New playbook"}</h1>
          <p>{dirty ? "Unsaved changes" : "Local library"}</p>
        </div>
        <div className={styles.actions}>
          <button
            type="button"
            className={buttons.primary}
            disabled={busy || (conflict && !compared)}
            onClick={() => void save()}
          >
            {busy
              ? "Working…"
              : compared
                ? "Save compared draft"
                : "Save playbook"}
          </button>
        </div>
      </div>
      {notice && <p role="status">{notice}</p>}
      {error && (
        <div className={styles.error} role="alert">
          {error}
          {errorField && (
            <button
              type="button"
              className={buttons.ghost}
              onClick={showErrorField}
            >
              Go to field: {errorField}
            </button>
          )}
        </div>
      )}
      {conflict && (
        <section className={styles.conflict} aria-label="Save conflict">
          <h2>The saved playbook changed</h2>
          <p>
            Your complete draft is still here. Compare it with the current
            version before saving, keep it as a new playbook, or discard it.
          </p>
          <div className={styles.actions}>
            {original && (
              <button
                className={buttons.ghost}
                disabled={busy}
                onClick={() => void current(false)}
              >
                Compare with my draft
              </button>
            )}
            <button
              className={buttons.ghost}
              disabled={busy}
              onClick={() => void save(true)}
            >
              Save my draft as new playbook
            </button>
            {original && (
              <button
                className={buttons.ghost}
                disabled={busy}
                onClick={() =>
                  setRemove({
                    title: "Discard my draft and load current playbook?",
                    apply: () => void current(true),
                  })
                }
              >
                Discard my draft…
              </button>
            )}
          </div>
          {compared && (
            <>
              <p>
                Saving replaces the current bundle with your draft. Review all
                differences below; nothing has been saved yet. Edited TOML files
                are rebuilt from their fields, so their source comments and
                formatting are not retained.
              </p>
              {differences(draft, fromDetail(compared)).map((diff) => (
                <details key={diff.path} open>
                  <summary>{diff.path}</summary>
                  <div className={styles.comparison}>
                    <div>
                      <h3>Current saved version</h3>
                      <pre>{diff.current}</pre>
                    </div>
                    <div>
                      <h3>My draft</h3>
                      <pre>{diff.mine}</pre>
                    </div>
                  </div>
                </details>
              ))}
            </>
          )}
        </section>
      )}
      <PlaybookValidation.Provider
        value={{ field: errorField, message: error }}
      >
        <fieldset disabled={busy} style={{ border: 0, margin: 0, padding: 0 }}>
          <div className={styles.fields}>
            <TextField
              label="Playbook name"
              value={manifest.identity.name}
              onChange={(v) => identity("name", v)}
              field="identity.name"
            />
            <TextField
              label="Playbook ID"
              value={manifest.identity.id}
              onChange={(v) => identity("id", v)}
              readOnly={!!original}
              field="identity.id"
            />
            <TextField
              label="Publisher"
              value={manifest.identity.publisher}
              onChange={(v) => identity("publisher", v)}
              field="identity.publisher"
            />
            <TextField
              label="Version"
              value={manifest.identity.version}
              onChange={(v) => identity("version", v)}
              field="identity.version"
            />
            <TextField
              label="Domain"
              value={manifest.identity.domain}
              onChange={(v) => identity("domain", v)}
              field="identity.domain"
            />
            <TextField
              label="Summary"
              value={manifest.identity.summary ?? ""}
              onChange={(v) => identity("summary", v)}
              field="identity.summary"
            />
          </div>
          <details>
            <summary>README</summary>
            <TextField
              label="README (Markdown)"
              multiline
              value={draft.readme}
              onChange={(readme) => edit({ ...draft, readme })}
            />
          </details>
          <PlaybookVariables
            variables={manifest.variables ?? []}
            schema={schema}
            onChange={(variables) =>
              document(MANIFEST, { ...manifest, variables })
            }
          />
          <div
            className={`${styles.workspace} ${canvas ? styles.canvasWorkspace : ""}`}
          >
            <section aria-label="Flow steps">
              <h2 tabIndex={-1}>Flow steps</h2>
              {!mobile && (
                <div className={styles.actions} aria-label="Flow view">
                  <button
                    type="button"
                    className={
                      view === "canvas" ? buttons.primary : buttons.ghost
                    }
                    aria-pressed={view === "canvas"}
                    onClick={() => setView("canvas")}
                  >
                    Canvas
                  </button>
                  <button
                    type="button"
                    className={
                      view === "list" ? buttons.primary : buttons.ghost
                    }
                    aria-pressed={view === "list"}
                    onClick={() => setView("list")}
                  >
                    List
                  </button>
                </div>
              )}
              <Choice
                label="Flow"
                value={path}
                choices={paths.map((p) => ({
                  value: p,
                  label: `${(draft.documents[p] as Flow).title} (${flowId(p)})`,
                }))}
                onChange={(p) => {
                  setPath(p);
                  setSelected("");
                }}
              />
              <details>
                <summary>Flow settings</summary>
                <Choice
                  label="Default flow"
                  value={manifest.flows?.default ?? ""}
                  choices={paths.map(flowId)}
                  optional
                  onChange={(value) =>
                    document(MANIFEST, {
                      ...manifest,
                      flows: value ? { default: value } : {},
                    })
                  }
                />
                <button
                  type="button"
                  className={buttons.ghost}
                  disabled={paths.length >= schema.limits.flows}
                  onClick={() => {
                    const p = flowPath(freshId("flow"));
                    document(p, {
                      format: manifest.format,
                      title: "New flow",
                      steps: [newStep()],
                    });
                    setPath(p);
                    setSelected("");
                  }}
                >
                  Add flow
                </button>
                {flow && (
                  <>
                    <TextField
                      label="Flow title"
                      value={flow.title}
                      onChange={(title) => document(path, { ...flow, title })}
                      field="title"
                    />
                    <TextField
                      label="Flow description"
                      value={flow.description ?? ""}
                      onChange={(description) =>
                        document(path, { ...flow, description })
                      }
                    />
                  </>
                )}
              </details>
              {flow && (
                <>
                  {canvas && (
                    <PlaybookFlowCanvas
                      key={path}
                      steps={flow.steps}
                      selected={selectedStep?.id}
                      onSelect={selectStep}
                      onChange={
                        busy
                          ? undefined
                          : (steps) => document(path, { ...flow, steps })
                      }
                    />
                  )}
                  <ol className={styles.steps} hidden={canvas}>
                    {flow.steps.map((s, index) => (
                      <li key={s.id} data-selected={selectedStep?.id === s.id}>
                        <button
                          type="button"
                          className={`${buttons.ghost} ${styles.selectStep}`}
                          aria-pressed={selectedStep?.id === s.id}
                          onClick={() => selectStep(s.id)}
                        >
                          {index + 1}. {s.title}
                        </button>
                        <PlaybookActor
                          step={{
                            ...s,
                            actor: s.actor as Parameters<
                              typeof PlaybookActor
                            >[0]["step"]["actor"],
                            after: s.after ?? [],
                            note:
                              s.actor.kind === "none" && !s.checklist?.length,
                          }}
                        />
                        <span className={styles.stepMeta}>
                          After:{" "}
                          {(s.after ?? [])
                            .map(
                              (id) =>
                                flow.steps.find((p) => p.id === id)?.title ??
                                id,
                            )
                            .join(", ") || "Start"}
                        </span>
                        <div className={styles.actions}>
                          {([-1, 1] as const).map((direction) => (
                            <button
                              key={direction}
                              type="button"
                              className={buttons.icon}
                              aria-label={`Move ${s.title} ${direction < 0 ? "up" : "down"}`}
                              disabled={
                                index + direction < 0 ||
                                index + direction >= flow.steps.length
                              }
                              onClick={() => {
                                const steps = [...flow.steps];
                                [steps[index], steps[index + direction]] = [
                                  steps[index + direction],
                                  steps[index],
                                ];
                                document(path, { ...flow, steps });
                              }}
                            >
                              {direction < 0 ? "↑" : "↓"}
                            </button>
                          ))}
                        </div>
                      </li>
                    ))}
                  </ol>
                  <p hidden={canvas}>
                    Order controls presentation. Dependencies determine when a
                    step can begin.
                  </p>
                  <div className={styles.actions}>
                    <button
                      type="button"
                      className={buttons.ghost}
                      disabled={flow.steps.length >= schema.limits.steps}
                      onClick={() => {
                        const s = newStep();
                        document(path, { ...flow, steps: [...flow.steps, s] });
                        selectStep(s.id);
                      }}
                    >
                      Add step
                    </button>
                    <button
                      type="button"
                      className={buttons.ghost}
                      onClick={() =>
                        setRemove({
                          title: `Remove flow ${flow.title}?`,
                          apply: () => {
                            const documents = { ...draft.documents };
                            delete documents[path];
                            const files = { ...draft.files };
                            delete files[path];
                            if (manifest.flows?.default === flowId(path))
                              documents[MANIFEST] = { ...manifest, flows: {} };
                            edit({ ...draft, documents, files });
                            setPath(paths.find((p) => p !== path) ?? "");
                          },
                        })
                      }
                    >
                      Remove flow…
                    </button>
                  </div>
                </>
              )}
            </section>
            {flow && selectedStep && (
              <PlaybookStepEditor
                onBack={showSteps}
                key={`${path}:${selectedStep.id}`}
                step={selectedStep}
                steps={flow.steps}
                variables={manifest.variables ?? []}
                schema={schema}
                field={`steps[${flow.steps.indexOf(selectedStep)}]`}
                onChange={(s) =>
                  document(path, {
                    ...flow,
                    steps: flow.steps.map((old) => (old.id === s.id ? s : old)),
                  })
                }
                onRemove={() =>
                  setRemove({
                    title: `Remove step ${selectedStep.title}?`,
                    apply: () => {
                      document(path, {
                        ...flow,
                        steps: flow.steps.filter(
                          (s) => s.id !== selectedStep.id,
                        ),
                      });
                      setSelected("");
                    },
                  })
                }
              />
            )}
          </div>
          <details>
            <summary>
              Retained bundle files ({Object.keys(draft.files).length})
            </summary>
            <p>
              Materials, runbooks and templates stay in the bundle. This editor
              changes identity, README, variables and flow documents.
            </p>
            <ul>
              {Object.keys(draft.files)
                .sort()
                .map((p) => (
                  <li key={p}>{p}</li>
                ))}
            </ul>
          </details>
        </fieldset>
      </PlaybookValidation.Provider>
      {remove && (
        <PlaybookDialog
          tag="Playbook editor"
          title={remove.title}
          confirmLabel="Confirm"
          danger
          onCancel={() => setRemove(null)}
          onConfirm={() => {
            remove.apply();
            setRemove(null);
          }}
        >
          <p>
            Other references are retained. Any that become invalid must be
            corrected before saving.
          </p>
        </PlaybookDialog>
      )}
      {blocker.state === "blocked" && (
        <PlaybookDialog
          tag="Unsaved draft"
          title="Leave this playbook?"
          confirmLabel="Discard draft and leave"
          busy={busy}
          danger
          onCancel={() => blocker.reset()}
          onConfirm={() => blocker.proceed()}
        >
          <p>Your unsaved changes will be discarded. Cancel to keep editing.</p>
        </PlaybookDialog>
      )}
    </>
  );
}
