/** One bundle draft, explicit workflow/step Properties, and revision-fenced recovery (#1392). */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  Link,
  useBlocker,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import { ApiError, api } from "../lib/api";
import { PLAYBOOKS_PATH, playbookEditPath } from "../lib/routes";
import { useAskPanel } from "../components/ask/askPanel";
import { PlaybookValidation } from "../components/playbooks/playbookValidation";
import { PlaybookDialog } from "../components/playbooks/PlaybookDialog";
import {
  PlaybookStepEditor,
  TextField,
  Choice,
} from "../components/playbooks/PlaybookStepEditor";
import { PlaybookVariables } from "../components/playbooks/PlaybookVariables";
import {
  errorText,
  usePlaybookRead,
} from "../components/playbooks/usePlaybooks";
import {
  MANIFEST,
  ROLE_FORMAT,
  differences,
  draftFiles,
  flowId,
  flowPath,
  freshId,
  fromDetail,
  newDraft,
  newStep,
  same,
  upgradeSteps,
  upgradeToRoles,
  withStep,
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
  const ask = useAskPanel();
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
  const workspace = useRef<HTMLDivElement>(null);
  const lastNode = useRef("");
  const [selectionEpoch, setSelectionEpoch] = useState(0);
  const [focusRequest, setFocusRequest] = useState<{
    target: "graph" | "workflow" | "step";
  } | null>(null);
  useLayoutEffect(() => {
    if (!focusRequest) return;
    const node =
      focusRequest.target === "graph" && lastNode.current
        ? workspace.current?.querySelector<HTMLElement>(
            `.react-flow__node[data-id="${CSS.escape(lastNode.current)}"]`,
          )
        : null;
    const selector =
      focusRequest.target === "step"
        ? '[aria-label="Step properties"] h2'
        : focusRequest.target === "workflow"
          ? '[aria-label="Workflow properties"] h2'
          : '[aria-label="Flow steps"] h2';
    const heading = workspace.current?.querySelector<HTMLElement>(selector);
    const visible = (el: HTMLElement) =>
      el.getClientRects().length > 0 &&
      getComputedStyle(el).visibility !== "hidden";
    const focus = (el?: HTMLElement | null) => {
      el?.focus({ preventScroll: true });
      el?.scrollIntoView({ block: "nearest" });
    };
    if (node && visible(node)) {
      focus(node);
      return;
    }
    focus(heading);
    if (!node) return;
    // A step added while the mobile chart was hidden needs its first measurement.
    // Keep focus on the chart heading until ready, without stealing a later Tab/click.
    const observer = new MutationObserver(() => {
      if (window.document.activeElement !== heading) {
        observer.disconnect();
        return;
      }
      if (visible(node)) {
        focus(node);
        observer.disconnect();
      }
    });
    observer.observe(node, { attributes: true, attributeFilter: ["style"] });
    return () => observer.disconnect();
  }, [focusRequest]);
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
  const [reverting, setReverting] = useState(false);
  // #1654: only a book upgraded from a retained format ≤4 copy can revert (the server says).
  const [revertible, setRevertible] = useState(!!original?.revertible);
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
  const selectedStep = flow?.steps.find((s) => s.id === selected);
  function edit(next: Draft) {
    setDraft(next);
    setNotice("");
    setError("");
    setErrorField("");
  }
  function selectWorkflow(restoreNode = true) {
    setSelected("");
    setSelectionEpoch((epoch) => epoch + 1);
    setFocusRequest({ target: restoreNode ? "graph" : "workflow" });
  }
  function selectStep(id: string) {
    setSelectionEpoch((epoch) => epoch + 1);
    lastNode.current = id;
    setSelected(id);
    setFocusRequest({ target: "step" });
  }

  function addStep() {
    if (!flow) return;
    const step = newStep();
    document(path, { ...flow, steps: [...flow.steps, step] });
    selectStep(step.id);
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
    setRevertible(!!pb.revertible);
    setConflict(false);
    setCompared(null);
    setError("");
    setErrorField("");
    setSelected("");
    lastNode.current = "";
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
  /** #1654: restore the retained format ≤4 copy; the format 5 tree is itself retained. */
  async function revertFormat() {
    if (!original || busyRef.current || !revision) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      const pb = await api.revertPlaybookFormat(original.id, revision);
      if (!live.current) return;
      adopt(pb);
      // Hermes 6958: promise the recovery copy only when the server confirmed it.
      setNotice(
        pb.durable === false || pb.recovery_durable === false
          ? "Reverted to the retained format 4 copy, but durable storage or the recovery copy of the format 5 version could not be confirmed. Inspect the store before further changes."
          : "Reverted to the retained format 4 copy. The format 5 version is kept in the recovery area.",
      );
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
    setSelected("");
    if (match) {
      const f = draft.documents[errorPath ?? path] as Flow;
      const step = f?.steps[Number(match[1])];
      if (step) {
        lastNode.current = step.id;
        setSelected(step.id);
      }
    }
    requestAnimationFrame(() => {
      const field = [
        ...window.document.querySelectorAll<HTMLElement>("[data-field]"),
      ].find((el) => errorField.endsWith(el.dataset.field ?? "!"));
      if (field) {
        for (
          let parent = field.parentElement;
          parent;
          parent = parent.parentElement
        )
          if (parent instanceof HTMLDetailsElement) parent.open = true;
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
          <p>
            {dirty ? "Unsaved changes" : "Local library"} · Saving defines the
            workflow; it does not run it.
          </p>
          {/* AI authoring lives in HQ (#1611): HQ drafts or changes a playbook on a card the
              operator confirms, through this editor's own save route. */}
          <p className={styles.hint} data-testid="playbook-ask-hq">
            Want a draft or a change written for you?{" "}
            <button
              type="button"
              className={styles.linkButton}
              onClick={ask.openAsk}
            >
              Ask HQ
            </button>
          </p>
        </div>
        <div className={styles.actions}>
          {original &&
            revertible &&
            ((baseline.documents[MANIFEST] as Manifest | undefined)?.format ??
              0) >= ROLE_FORMAT && (
              <button
                type="button"
                className={buttons.ghost}
                disabled={busy || dirty}
                title={dirty ? "Save or discard your changes first" : undefined}
                onClick={() => setReverting(true)}
              >
                Revert to format 4…
              </button>
            )}
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
              label="Summary"
              value={manifest.identity.summary ?? ""}
              onChange={(v) => identity("summary", v)}
              field="identity.summary"
            />
          </div>
          <details>
            <summary>Advanced playbook properties</summary>
            <div className={styles.fields}>
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
            </div>
          </details>
          <details>
            <summary>README</summary>
            <TextField
              label="README (Markdown)"
              multiline
              value={draft.readme}
              onChange={(readme) => edit({ ...draft, readme })}
            />
          </details>
          <div className={styles.workflowBar}>
            <Choice
              label="Workflow"
              value={path}
              choices={paths.map((p) => ({
                value: p,
                label: (draft.documents[p] as Flow).title,
              }))}
              onChange={(p) => {
                setPath(p);
                setSelected("");
                lastNode.current = "";
              }}
            />
            <div className={styles.actions}>
              <button
                type="button"
                className={buttons.ghost}
                disabled={paths.length >= schema.limits.flows}
                onClick={() => {
                  const p = flowPath(freshId("flow"));
                  document(p, {
                    format: manifest.format,
                    title: "New workflow",
                    steps: [],
                  });
                  setPath(p);
                  setSelected("");
                  lastNode.current = "";
                }}
              >
                Add workflow
              </button>
              <button
                type="button"
                className={buttons.ghost}
                disabled={!flow || flow.steps.length >= schema.limits.steps}
                onClick={addStep}
              >
                Add step
              </button>
              <button
                type="button"
                className={buttons.ghost}
                onClick={() => selectWorkflow(false)}
              >
                Workflow properties
              </button>
            </div>
          </div>
          <p role="status" className={styles.hint}>
            {flow ? `Workflow: ${flow.title}` : "No workflow selected"}
            {flow && selectedStep
              ? ` · Step ${flow.steps.indexOf(selectedStep) + 1} of ${flow.steps.length}: ${selectedStep.title}`
              : " · Workflow properties"}
          </p>
          <div
            ref={workspace}
            className={styles.workspace}
            onKeyDownCapture={(event) => {
              if (
                event.key === "Escape" &&
                !event.defaultPrevented &&
                !(
                  event.target instanceof Element &&
                  event.target.closest("dialog")
                )
              ) {
                event.preventDefault();
                event.stopPropagation();
                selectWorkflow();
              }
            }}
          >
            <section
              aria-label="Flow steps"
              className={`${styles.graph} ${selectedStep ? styles.mobileGraphHidden : ""}`}
            >
              <h2 tabIndex={-1} data-field="steps">
                Flowchart
              </h2>
              <p className={styles.hint}>
                Select a step to edit its Properties. Select the background for
                workflow properties.
              </p>
              {flow ? (
                <PlaybookFlowCanvas
                  key={path}
                  steps={flow.steps}
                  selected={selectedStep?.id}
                  selectionEpoch={selectionEpoch}
                  onSelect={selectStep}
                  onBackground={() => selectWorkflow(false)}
                  onChange={
                    busy
                      ? undefined
                      : (steps) => document(path, { ...flow, steps })
                  }
                />
              ) : (
                <p>Add a workflow to start.</p>
              )}
            </section>
            {flow && selectedStep ? (
              <PlaybookStepEditor
                onBack={() => selectWorkflow()}
                key={`${path}:${selectedStep.id}`}
                step={selectedStep}
                format={flow.format}
                upgradeSteps={upgradeSteps(draft)}
                onUpgrade={(picks) => edit(upgradeToRoles(draft, picks))}
                steps={flow.steps}
                variables={manifest.variables ?? []}
                schema={schema}
                field={`steps[${flow.steps.indexOf(selectedStep)}]`}
                onChange={(s) => edit(withStep(draft, path, s))}
                onMove={(direction) => {
                  const steps = [...flow.steps],
                    index = steps.indexOf(selectedStep);
                  [steps[index], steps[index + direction]] = [
                    steps[index + direction],
                    steps[index],
                  ];
                  document(path, { ...flow, steps });
                }}
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
                      lastNode.current = "";
                      selectWorkflow();
                    },
                  })
                }
              />
            ) : (
              <section
                className={styles.inspector}
                aria-label="Workflow properties"
              >
                <h2 tabIndex={-1}>Workflow properties</h2>
                <p>
                  Choose a step in the graph to edit its performer,
                  instructions, completion rules and next steps.
                </p>
                {flow && (
                  <>
                    <TextField
                      label="Workflow title"
                      value={flow.title}
                      field="title"
                      onChange={(title) => document(path, { ...flow, title })}
                    />
                    <TextField
                      label="Workflow description"
                      multiline
                      value={flow.description ?? ""}
                      field="description"
                      onChange={(description) =>
                        document(path, { ...flow, description })
                      }
                    />
                  </>
                )}
                <h3>Inputs</h3>
                <p className={styles.hint}>
                  These definitions are shared by this playbook’s workflows.
                  Values are supplied when assigning the playbook to a project.
                </p>
                <PlaybookVariables
                  variables={manifest.variables ?? []}
                  schema={schema}
                  onChange={(variables) =>
                    document(MANIFEST, { ...manifest, variables })
                  }
                />
                <details>
                  <summary>Advanced workflow properties</summary>
                  {flow && (
                    <TextField
                      label="Workflow ID"
                      value={flowId(path)}
                      readOnly
                    />
                  )}
                  <Choice
                    label="Default workflow"
                    value={manifest.flows?.default ?? ""}
                    choices={paths.map((p) => ({
                      value: flowId(p),
                      label: (draft.documents[p] as Flow).title,
                    }))}
                    optional
                    field="flows.default"
                    onChange={(value) =>
                      document(MANIFEST, {
                        ...manifest,
                        flows: value ? { default: value } : {},
                      })
                    }
                  />
                </details>
                {flow && (
                  <button
                    type="button"
                    className={buttons.ghost}
                    onClick={() =>
                      setRemove({
                        title: `Remove workflow ${flow.title}?`,
                        apply: () => {
                          const documents = { ...draft.documents };
                          delete documents[path];
                          const files = { ...draft.files };
                          delete files[path];
                          if (manifest.flows?.default === flowId(path))
                            documents[MANIFEST] = { ...manifest, flows: {} };
                          edit({ ...draft, documents, files });
                          setPath(paths.find((p) => p !== path) ?? "");
                          lastNode.current = "";
                          selectWorkflow(false);
                        },
                      })
                    }
                  >
                    Remove workflow…
                  </button>
                )}
              </section>
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
      {reverting && (
        <PlaybookDialog
          tag="Playbook editor"
          title="Revert to format 4?"
          confirmLabel="Revert"
          onCancel={() => setReverting(false)}
          onConfirm={() => {
            setReverting(false);
            void revertFormat();
          }}
        >
          <p>
            The format 4 copy this playbook was upgraded from is saved back
            exactly as it was. This format 5 version is not deleted: it is kept
            in the recovery area.
          </p>
        </PlaybookDialog>
      )}
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
