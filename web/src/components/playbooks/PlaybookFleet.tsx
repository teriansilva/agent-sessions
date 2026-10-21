import { useCallback, useState } from "react";
import { ApiError, api } from "../../lib/api";
import type {
  PlaybookFleetResult,
  PlaybookVerify,
} from "../../types/playbooks";
import { PlaybookDialog } from "./PlaybookDialog";
import { errorText, usePlaybookRead } from "./usePlaybooks";
import buttons from "../ui/actionButton.module.css";
import styles from "./playbooks.module.css";

type Attempt = { digest: string; operationId: string };
const attemptKey = (id: string) => `battlelab.playbook-fleet.${id}`;
function savedAttempt(id: string): Attempt | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(attemptKey(id)) || "null");
    return value &&
      /^[a-f0-9]{64}$/.test(value.digest) &&
      /^[a-f0-9-]{36}$/.test(value.operationId)
      ? value
      : null;
  } catch {
    return null;
  }
}

export function PlaybookFleetView({ id }: { id: string }) {
  const fleet = usePlaybookRead(
    useCallback(() => api.playbookProjects(id), [id]),
  );
  const names = usePlaybookRead(useCallback(() => api.projectEntities(), []));
  const [opening, setOpening] = useState<HTMLElement | null>(null);
  const [notice, setNotice] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(() =>
    savedAttempt(id),
  );
  function remember(next: Attempt | null) {
    // Persist only the operation identity, never file contents, variables or the review diff.
    // If browser storage refuses the write, do not submit an update whose retry would be lost.
    if (next) sessionStorage.setItem(attemptKey(id), JSON.stringify(next));
    else sessionStorage.removeItem(attemptKey(id));
    setAttempt(next);
  }
  const name = (pid: string) =>
    names.data?.projects.find((p) => p.id === pid)?.name || pid;
  return (
    <section className={styles.section}>
      <div className={styles.header}>
        <div>
          <h2>Projects using this playbook</h2>
          <p>
            Each project stays on its deployed revision until you apply an
            update.
          </p>
        </div>
        <button
          className={buttons.primary}
          disabled={
            !attempt &&
            (!fleet.data?.projects.some((p) => p.update_available) ||
              !!fleet.error ||
              fleet.loading)
          }
          onClick={(e) => setOpening(e.currentTarget)}
        >
          {attempt ? "Resume reviewed update…" : "Update projects…"}
        </button>
      </div>
      {notice && <p role="status">{notice}</p>}
      {fleet.loading && !fleet.data && <p>Loading project deployments…</p>}
      {fleet.error && (
        <div role="alert">
          {fleet.error}
          <button className={buttons.ghost} onClick={() => void fleet.reload()}>
            Reload projects
          </button>
        </div>
      )}
      {fleet.data && !fleet.data.projects.length && (
        <p>No projects run this playbook.</p>
      )}
      <div className={styles.fleet}>
        {fleet.data?.projects.map((p) => (
          <ProjectRow
            key={`${p.project_id}:${p.deployment_id}:${p.revision}`}
            row={p}
            name={name(p.project_id)}
          />
        ))}
      </div>
      {opening && (
        <FleetUpdate
          id={id}
          attempt={attempt}
          remember={remember}
          name={name}
          returnFocusTo={opening}
          onClose={() => setOpening(null)}
          onDone={(message) => {
            setNotice(message);
            setOpening(null);
            void fleet.reload();
          }}
        />
      )}
    </section>
  );
}

function ProjectRow({
  row,
  name,
}: {
  row: {
    project_id: string;
    deployment_id: string;
    state: string;
    revision: string | null;
    update_available: boolean;
  };
  name: string;
}) {
  const [result, setResult] = useState<PlaybookVerify | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function verify() {
    setBusy(true);
    setError("");
    try {
      const r = await api.verifyPlaybook(row.project_id);
      if (r.deployment_id !== row.deployment_id) {
        setResult(null);
        setError("The deployment changed. Reload projects before verifying.");
      } else setResult(r);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <article className={styles.project} data-testid={`fleet-${row.project_id}`}>
      <div>
        <strong>{name}</strong>
        <p>
          {row.state} ·{" "}
          <span className={row.update_available ? styles.warn : ""}>
            {row.update_available ? "Update available" : "Current revision"}
          </span>
        </p>
        <p title={row.revision || "Revision unavailable"}>
          Deployed revision:{" "}
          <code>{row.revision?.slice(0, 12) || "not recorded"}</code>
        </p>
      </div>
      <div>
        <p
          className={
            error || (result && !result.ok)
              ? styles.warn
              : result
                ? styles.good
                : ""
          }
        >
          {error
            ? "Verification unavailable"
            : result
              ? result.ok
                ? "Verified"
                : "Needs attention"
              : "Not verified"}
        </p>
        {result?.checks.materials ? (
          <p>
            {error ? "Previous drift result" : "Drift"}:{" "}
            {result.checks.materials.drift?.join(", ") ||
              (result.checks.materials.ok ? "none" : "unknown")}
          </p>
        ) : (
          <p>Drift not checked</p>
        )}
        {result &&
          Object.entries(result.checks)
            .filter(([k, v]) => k !== "materials" && v.ok === false)
            .map(([k, v]) => (
              <p key={k}>
                {k}: {v.missing?.join(", ") || v.error || "needs attention"}
              </p>
            ))}
        {error && <p role="alert">{error}</p>}
      </div>
      <button
        className={buttons.ghost}
        disabled={busy || row.state !== "applied"}
        onClick={() => void verify()}
      >
        {busy ? "Verifying…" : "Verify"}
      </button>
    </article>
  );
}

function FleetUpdate({
  id,
  attempt,
  remember,
  name,
  returnFocusTo,
  onClose,
  onDone,
}: {
  id: string;
  name: (id: string) => string;
  returnFocusTo: HTMLElement;
  attempt: Attempt | null;
  remember: (next: Attempt | null) => void;
  onClose: () => void;
  onDone: (notice: string) => void;
}) {
  const review = usePlaybookRead(
    useCallback(
      () => (attempt ? Promise.resolve(null) : api.reviewPlaybookFleet(id)),
      [id, attempt],
    ),
  );
  const observed = usePlaybookRead(
    useCallback(
      () =>
        attempt
          ? api.playbookFleetOperation(id, attempt.operationId)
          : Promise.resolve(null),
      [id, attempt],
    ),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<PlaybookFleetResult | null>(null);
  const [expanded, setExpanded] = useState<string[]>([]);
  // Once sent, retries keep both the exact reviewed digest and operation id, even after a lost reply.
  const plan = review.data;
  const outcomes = result ?? observed.data;
  const complete =
    !!outcomes &&
    outcomes.projects.every(
      (p) => p.outcome === "applied" || p.outcome === "stale",
    );
  const eligible = plan?.projects.filter((p) => p.batchable) ?? [];
  const describe = (value: PlaybookFleetResult) =>
    value.projects.length
      ? value.projects
          .map(
            (p) =>
              `${name(p.project_id)}: ${p.outcome}${p.detail ? ` — ${p.detail}` : ""}`,
          )
          .join("; ")
      : "No projects were updated.";
  function finish(value: PlaybookFleetResult) {
    if (
      value.projects.some(
        (p) => p.outcome === "failed" || p.outcome === "not-attempted",
      )
    ) {
      setResult(value);
      return;
    }
    remember(null);
    onDone(describe(value));
  }
  async function apply() {
    if ((!plan && !attempt) || busy) return;
    const current = attempt ?? {
      digest: plan!.digest,
      operationId: crypto.randomUUID(),
    };
    setBusy(true);
    setError("");
    try {
      remember(current);
    } catch {
      setError(
        "Browser storage is unavailable. Enable session storage before applying so this update can be recovered.",
      );
      setBusy(false);
      return;
    }
    try {
      finish(
        await api.updatePlaybookFleet(id, current.digest, current.operationId),
      );
    } catch (e) {
      // A dropped response may have followed a committed update; observe before offering a retry.
      try {
        const settled = await api.playbookFleetOperation(
          id,
          current.operationId,
        );
        if (
          settled.projects.every(
            (p) => p.outcome === "applied" || p.outcome === "stale",
          )
        ) {
          finish(settled);
          return;
        }
        setResult(settled);
      } catch (observationError) {
        // A definite refusal before any journal existed needs a fresh, separately approved review.
        if (
          e instanceof ApiError &&
          e.status === 409 &&
          observationError instanceof ApiError &&
          observationError.status === 404
        ) {
          remember(null);
          setResult(null);
          setError(
            `${errorText(e)} Review the current changes before applying again.`,
          );
          return;
        }
      }
      setError(
        `${errorText(e)} Retry uses the same reviewed update and operation id.`,
      );
    } finally {
      setBusy(false);
    }
  }
  const confirm = attempt
    ? complete
      ? "Close completed update"
      : "Retry reviewed update"
    : !plan
      ? "Try review again"
      : !eligible.length
        ? "Close review"
        : `Apply to ${eligible.length} project${eligible.length === 1 ? "" : "s"}`;
  return (
    <PlaybookDialog
      tag="Update projects"
      title="Review project updates"
      returnFocusTo={returnFocusTo}
      busy={busy || review.loading || observed.loading}
      confirmLabel={confirm}
      onCancel={onClose}
      onConfirm={() =>
        attempt
          ? complete
            ? finish(outcomes!)
            : void apply()
          : !plan
            ? void review.reload()
            : !eligible.length
              ? onClose()
              : void apply()
      }
    >
      <div className={styles.review}>
        <p>
          Only the projects marked ready below are included. Eligibility is
          checked again when applying. Projects that need their own review stay
          unchanged.
        </p>
        {review.loading && <p>Preparing the combined review…</p>}
        {(review.error || error) && (
          <p role="alert" className={styles.error}>
            {error || review.error}
          </p>
        )}
        {attempt && (
          <>
            <p>
              This update was already submitted. A retry resumes only its
              reviewed projects. Closing this dialog retains it in this browser
              tab.
            </p>
            {observed.error && (
              <p role="alert">
                Could not read the recorded outcome: {observed.error}
              </p>
            )}
            {outcomes && <p role="status">{describe(outcomes)}</p>}
            <button
              className={buttons.ghost}
              disabled={busy || observed.loading}
              onClick={() => {
                setResult(null);
                void observed.reload();
              }}
            >
              Check recorded outcome
            </button>
          </>
        )}
        {!attempt &&
          plan?.projects.map((p) => (
            <section key={p.project_id}>
              <h3>{name(p.project_id)}</h3>
              <p className={p.batchable ? styles.good : styles.warn}>
                {p.batchable ? "Ready for this update" : "Needs its own review"}
              </p>
              {!!p.reasons.length && (
                <ul>
                  {p.reasons.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              )}
              {!!p.changes?.length && (
                <>
                  <button
                    className={buttons.ghost}
                    onClick={() =>
                      setExpanded((old) =>
                        old.includes(p.project_id)
                          ? old.filter((v) => v !== p.project_id)
                          : [...old, p.project_id],
                      )
                    }
                  >
                    {expanded.includes(p.project_id) ? "Hide" : "Show"} changes
                    for {name(p.project_id)}
                  </button>
                  {expanded.includes(p.project_id) &&
                    p.changes.map((change, i) => (
                      <div key={`${change.path}:${i}`}>
                        <p>
                          <code>{change.path}</code> · {change.action}
                        </p>
                        <pre>
                          {change.diff ||
                            JSON.stringify(
                              { before: change.before, after: change.after },
                              null,
                              2,
                            )}
                        </pre>
                      </div>
                    ))}
                </>
              )}
            </section>
          ))}
        {plan && !plan.projects.length && (
          <p>All projects are already current.</p>
        )}
      </div>
    </PlaybookDialog>
  );
}
