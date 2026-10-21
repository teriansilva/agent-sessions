/** Explicit deployment removal, using the server's reviewed inverse and same-operation retry. */
import { useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import type {
  DeploymentRemoval,
  DeploymentStatus,
} from "../../types/playbookDeployment";
import { PlaybookDialog } from "./PlaybookDialog";
import { errorText } from "./usePlaybooks";
import buttons from "../ui/actionButton.module.css";
import styles from "./ProjectPlaybook.module.css";

export function ProjectPlaybookRemove({
  project,
  disabled,
  onBusy,
  onRemoved,
}: {
  project: string;
  disabled: boolean;
  onBusy: (busy: boolean) => void;
  onRemoved: (status: DeploymentStatus) => void;
}) {
  const [open, setOpen] = useState(false);
  const [plan, setPlan] = useState<DeploymentRemoval | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [trigger, setTrigger] = useState<HTMLElement | null>(null);
  const [operation, setOperation] = useState<string | null>(null);
  const live = useRef(true);
  const busyRef = useRef(false);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  async function review(button: HTMLElement) {
    if (busyRef.current || disabled) return;
    if (plan && operation) {
      setTrigger(button);
      setOpen(true);
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setError("");
    onBusy(true);
    try {
      const result = await api.reviewPlaybookRemoval(project);
      if (live.current) {
        setPlan(result);
        setTrigger(button);
        setOperation(null);
        setOpen(true);
      }
    } catch (e) {
      if (live.current) setError(errorText(e));
    } finally {
      busyRef.current = false;
      if (live.current) {
        setBusy(false);
        onBusy(false);
      }
    }
  }
  async function remove() {
    if (
      !plan ||
      busyRef.current ||
      plan.conflicts.length ||
      plan.bindings.refusal
    )
      return;
    busyRef.current = true;
    setBusy(true);
    onBusy(true);
    setError("");
    const id = operation ?? crypto.randomUUID();
    setOperation(id);
    try {
      const result = await api.removePlaybookDeployment(
        project,
        plan.digest,
        id,
      );
      if (live.current) {
        setPlan(null);
        onRemoved(result);
      }
    } catch (e) {
      if (live.current) setError(errorText(e));
    } finally {
      busyRef.current = false;
      if (live.current) {
        setBusy(false);
        onBusy(false);
      }
    }
  }
  return (
    <div className={styles.root}>
      <button
        className={buttons.ghost}
        disabled={disabled || busy}
        onClick={(e) => void review(e.currentTarget)}
      >
        {operation ? "Resume this removal" : "Review removal of this playbook"}
      </button>
      {error && !plan && <p role="alert">{error}</p>}
      {open && plan && (
        <PlaybookDialog
          tag="Playbook removal"
          title="Review removal"
          confirmLabel={
            operation ? "Retry this removal" : "Remove reviewed playbook"
          }
          danger
          busy={busy}
          confirmDisabled={!!plan.conflicts.length || !!plan.bindings.refusal}
          onCancel={() => setOpen(false)}
          onConfirm={() => void remove()}
          returnFocusTo={trigger}
        >
          <div className={styles.root}>
            <p>
              The project and its folders stay. Only files proven to belong to
              this deployment are removed or restored; project-owned seeds and
              reference files stay.
            </p>
            <p>
              Review <code className={styles.digest}>{plan.digest}</code>
            </p>
            {plan.changes.map((change) => (
              <details className={styles.card} key={change.path}>
                <summary>
                  {change.action} · {change.path}
                </summary>
                <pre>{change.diff || JSON.stringify(change, null, 2)}</pre>
              </details>
            ))}
            <p>
              Bindings removed: {plan.bindings.remove.join(", ") || "none"}.
              Previous bindings restored:{" "}
              {plan.bindings.restore.join(", ") || "none"}.
            </p>
            {!!plan.secret_files.length && (
              <p>
                Secret-reference files:{" "}
                {plan.secret_files.map((f) => f.name).join(", ")}. Only the
                deployment’s proven files can be cleared.
              </p>
            )}
            {!!plan.conflicts.length && (
              <p role="alert">
                Removal is refused:{" "}
                {plan.conflicts.map((c) => `${c.path}: ${c.reason}`).join("; ")}
              </p>
            )}
            {plan.bindings.refusal && (
              <p role="alert">{plan.bindings.refusal}</p>
            )}
            {error && (
              <p role="alert">
                {error} The result may be partial. Retry keeps the same
                operation.
              </p>
            )}
          </div>
        </PlaybookDialog>
      )}
    </div>
  );
}
