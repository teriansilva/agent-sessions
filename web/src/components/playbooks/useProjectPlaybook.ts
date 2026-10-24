/** New-project assignment: review is read-only; every retry keeps the accepted request and
 * operation IDs. This hook neither creates projects nor starts workflow agents. */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import type {
  PlaybookDetail,
  PlaybookList,
  PlaybookVerify,
} from "../../types/playbooks";
import { bindingRole } from "../../types/playbooks";
import type {
  DeploymentReceipt,
  DeploymentReview,
  DeploymentStatus,
  PlaybookAssignments,
  PlaybookBinding,
  PlaybookInputs,
} from "../../types/playbookDeployment";
import type { Manifest } from "./playbookDraft";
import { errorText } from "./usePlaybooks";

export type InputChoice = {
  mode: "inherit" | "global" | "value";
  value: string;
};
type CreatedTarget = { path: string; device: number; inode: number };
type Attempt = {
  id: string;
  inputs: PlaybookInputs;
  receipt: string;
  bindId: string;
  applyId: string;
  plan: DeploymentReview;
  confirmed: string[];
  bound?: DeploymentReceipt;
  applied?: boolean;
};
/** The agent steps a deployment may still assign (#1654): format ≤4 ones. A format 5 step names
 *  a role that is filled per mission, and the server refuses an assignment for it. */
export function assignableSteps(detail: PlaybookDetail): Set<string> {
  return new Set(
    (detail.flows ?? []).flatMap((f) =>
      f.steps
        .filter((s) => s.actor.kind === "agent" && !bindingRole(s))
        .map((s) => `${f.id}:${s.id}`),
    ),
  );
}

export function assignmentUnavailable(
  detail: PlaybookDetail,
  preProject = true,
): string {
  if (!detail.ok) return detail.error || "This playbook cannot be loaded.";
  if (
    preProject &&
    Object.values(detail.files ?? {}).some(
      (file) => typeof file === "string" && /\{\{\s*secret_path:/.test(file),
    )
  )
    return "This playbook writes secret-reference files and needs an existing project before review. Create the project with No playbook first.";
  return "";
}

const unconfirmedCreate =
  "Folder creation has not been confirmed. Retry Create with the original review before refreshing it.";

export function useProjectPlaybook(
  destination: string,
  createTarget: boolean,
  projectId?: string,
) {
  const [library, setLibrary] = useState<PlaybookList | null>(null);
  const [libraryError, setLibraryError] = useState("");
  const [id, setId] = useState<string | null>(null);
  const [detail, setDetail] = useState<PlaybookDetail | null>(null);
  const [loadError, setLoadError] = useState("");
  const [defaultNotice, setDefaultNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [agents, setAgents] = useState<string[]>([]);
  const [choices, setChoices] = useState<Record<string, InputChoice>>({});
  const [assignments, setAssignments] = useState<PlaybookAssignments>({});
  const [trust, setTrust] = useState(false);
  const [reviewed, setReviewed] = useState<{
    inputs: PlaybookInputs;
    plan: DeploymentReview;
  } | null>(null);
  const [confirmed, setConfirmed] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState("");
  const [recovering, setRecovering] = useState(false);
  const [stage, setStage] = useState<
    "idle" | "bind" | "apply" | "verify" | "done"
  >("idle");
  const [status, setStatus] = useState<DeploymentStatus | null>(null);
  const [verification, setVerification] = useState<PlaybookVerify | null>(null);
  const live = useRef(true);
  const epoch = useRef(0);
  const busyRef = useRef(false);
  const attempt = useRef<Attempt | null>(null);
  // Retained separately from a deployment attempt: a definite project-create refusal can
  // leave our folder on disk even when the source or inputs need a new review.
  const createdTargets = useRef(new Map<string, CreatedTarget>());
  // A lost target-create response binds its receipt to that folder only. Choosing another
  // folder parks it here; returning restores the original review for the retry.
  const unresolvedCreates = useRef(new Map<string, Attempt>());
  const [existingTargetPath, setExistingTargetPath] = useState<string | null>(null);
  const manuallySelected = useRef(false);
  const signature = JSON.stringify({
    id,
    revision: detail?.revision,
    destination,
    createTarget,
    choices,
    assignments,
  });
  const currentSignature = useRef(signature);
  useEffect(() => {
    currentSignature.current = signature;
  }, [signature]);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      attempt.current = null;
    };
  }, []);

  const loadSelection = useCallback(
    async (next: string | null, isDefault = false) => {
      const request = ++epoch.current;
      setId(next);
      setDetail(null);
      setLoadError("");
      setChoices({});
      setAssignments({});
      setTrust(false);
      setReviewed(null);
      setConfirmed([]);
      setError("");
      attempt.current = null;
      unresolvedCreates.current.clear();
      if (!next) {
        setLoading(false);
        return;
      }
      setLoading(true);
      try {
        const [data, schema] = await Promise.all([
          api.playbook(next),
          api.playbookAuthoring(),
        ]);
        if (!live.current || request !== epoch.current) return;
        setAgents(schema.agents);
        const reason = assignmentUnavailable(data);
        if (isDefault && reason) {
          setId(null);
          setDefaultNotice(
            `The configured default “${data.name || next}” is unavailable: ${reason} No playbook is selected.`,
          );
        } else {
          setDetail(data);
          setLoadError(reason);
        }
      } catch (e) {
        if (!live.current || request !== epoch.current) return;
        if (isDefault) {
          setId(null);
          setDefaultNotice(
            `The configured default “${next}” is unavailable. No playbook is selected.`,
          );
        } else setLoadError(errorText(e));
      } finally {
        if (live.current && request === epoch.current) setLoading(false);
      }
    },
    [],
  );
  const reloadLibrary = useCallback(async () => {
    try {
      const data = await api.playbooks();
      if (!live.current) return;
      setLibrary(data);
      setLibraryError("");
      if (!manuallySelected.current && data.default) {
        manuallySelected.current = true;
        const selected = data.playbooks.find(
          (p) => p.id === data.default && p.ok,
        );
        if (selected) await loadSelection(selected.id, true);
        else
          setDefaultNotice(
            `The configured default “${data.default}” is unavailable. No playbook is selected.`,
          );
      }
    } catch (e) {
      if (live.current) setLibraryError(errorText(e));
    }
  }, [loadSelection]);
  useEffect(() => {
    // The initial library read owns loading/default-selection state, like usePlaybookRead.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reloadLibrary();
  }, [reloadLibrary]); // one initial read; explicit retries preserve the operator's selection
  const manifest = detail?.documents?.["playbook.toml"] as Manifest | undefined;
  const variables = manifest?.variables ?? [];
  const bindings: PlaybookBinding[] = variables.flatMap((variable) => {
    const choice = choices[variable.name];
    if (!choice || choice.mode === "inherit") return [];
    const base = {
      name: variable.name,
      kind:
        variable.kind === "secret" ? ("secret" as const) : ("text" as const),
    };
    return [
      choice.mode === "global"
        ? { ...base, ref: "global" as const }
        : { ...base, value: choice.value },
    ];
  });
  const inputs: PlaybookInputs = {
    revision: detail?.revision ?? "",
    destination,
    ...(projectId
      ? { project_id: projectId }
      : createTarget && existingTargetPath !== destination
        ? { create: true }
        : {}),
    bindings,
    assignments,
  };
  const reviewCurrent =
    reviewed !== null &&
    JSON.stringify(reviewed.inputs) === JSON.stringify(inputs);
  const selectedReady =
    !id ||
    (!!detail?.revision &&
      !loading &&
      !loadError &&
      (detail.source !== "local" || trust));
  const canCreate =
    !id ||
    (selectedReady &&
      reviewCurrent &&
      !reviewed.plan.conflicts.length &&
      reviewed.plan.targets.every(
        (t) => !t.requires_confirmation || confirmed.includes(t.id),
      ));

  async function review(refreshSource = false) {
    if (!id || !selectedReady || busyRef.current) return;
    const target = createdTargets.current.get(destination);
    const held = attempt.current;
    if (held?.inputs.create && !createdTargets.current.has(held.inputs.destination)) {
      if (held.inputs.destination === destination) {
        setError(unconfirmedCreate);
        return;
      }
      unresolvedCreates.current.set(held.inputs.destination, held);
      attempt.current = null;
    }
    const parked = target ? undefined : unresolvedCreates.current.get(destination);
    if (parked) {
      unresolvedCreates.current.delete(destination);
      attempt.current = parked;
      setReviewed({ inputs: parked.inputs, plan: parked.plan });
      setConfirmed(parked.confirmed);
      setError(unconfirmedCreate);
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setError("");
    setReviewed(null);
    setConfirmed([]);
    const requestedSignature = currentSignature.current;
    let acceptedInputs = inputs;
    let fresh: PlaybookDetail | null = null;
    let retainedChoices = choices;
    let retainedAssignments = assignments;
    try {
      if (refreshSource) {
        fresh = await api.playbook(id);
        if (!live.current || requestedSignature !== currentSignature.current)
          return;
        const unavailable = assignmentUnavailable(fresh, !projectId);
        if (unavailable) throw new Error(unavailable);
        const declarations =
          (fresh.documents?.["playbook.toml"] as Manifest | undefined)
            ?.variables ?? [];
        const unchanged = new Set(
          declarations
            .filter((v) =>
              variables.some(
                (old) => old.name === v.name && old.kind === v.kind,
              ),
            )
            .map((v) => v.name),
        );
        retainedChoices = Object.fromEntries(
          Object.entries(choices).filter(([name]) => unchanged.has(name)),
        );
        // A refreshed revision may have upgraded to format 5: its role steps take no assignment.
        const assignable = assignableSteps(fresh);
        retainedAssignments = Object.fromEntries(
          Object.entries(assignments).filter(([key]) => assignable.has(key)),
        );
        acceptedInputs = {
          ...inputs,
          revision: fresh.revision || "",
          bindings: bindings.filter((v) => unchanged.has(v.name)),
          assignments: retainedAssignments,
        };
      }
      if (target && !projectId) {
        acceptedInputs = { ...acceptedInputs };
        delete acceptedInputs.create;
      }
      const plan = await api.reviewPlaybook(id, acceptedInputs);
      if (target && (plan.destination.path !== target.path ||
          plan.destination.device !== target.device || plan.destination.inode !== target.inode))
        throw new Error("The created folder was replaced. Choose another folder, or restart the wizard to explicitly review the replacement.");
      if (live.current && requestedSignature === currentSignature.current) {
        attempt.current = null;
        if (target) setExistingTargetPath(destination);
        if (fresh) {
          setDetail(fresh);
          setChoices(retainedChoices);
          setAssignments(retainedAssignments);
        }
        setReviewed({ inputs: acceptedInputs, plan });
      }
    } catch (e) {
      if (live.current && requestedSignature === currentSignature.current) {
        if (fresh) {
          setDetail(fresh);
          setChoices(retainedChoices);
          setAssignments(retainedAssignments);
        }
        setError(errorText(e));
      }
    } finally {
      busyRef.current = false;
      if (live.current) setBusy(false);
    }
  }

  /** Called immediately before project creation; confirmation is recomputed server-side. */
  async function prepare(): Promise<boolean> {
    if (!id) return true;
    if (!canCreate || !reviewed || busyRef.current) return false;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      let accepted = attempt.current;
      if (!accepted) {
        const signed = await api.confirmPlaybook(
          id,
          reviewed.inputs,
          reviewed.plan.digest,
          confirmed,
        );
        if (!live.current) return false;
        accepted = {
          id,
          inputs: reviewed.inputs,
          receipt: signed.receipt,
          bindId: crypto.randomUUID(),
          applyId: crypto.randomUUID(),
          plan: reviewed.plan,
          confirmed,
        };
        attempt.current = accepted;
      }
      if (JSON.stringify(accepted.inputs) !== JSON.stringify(reviewed.inputs)) {
        setError(
          "The inputs changed. Refresh the review before creating the project.",
        );
        return false;
      }
      if (accepted.inputs.create) {
        const result = await api.createPlaybookTarget(id, accepted.inputs, accepted.receipt);
        const target: Partial<CreatedTarget> | null = result;
        if (!target || typeof target.path !== "string" || typeof target.device !== "number" || typeof target.inode !== "number")
          throw new Error("The created folder identity was not returned. Retry with the original review.");
        createdTargets.current.set(accepted.inputs.destination, target as CreatedTarget);
      }
      return live.current;
    } catch (e) {
      if (live.current) setError(errorText(e));
      return false;
    } finally {
      busyRef.current = false;
      if (live.current) setBusy(false);
    }
  }

  /** All later retries use the same project and operation IDs, including lost responses. */
  async function deploy(project: string) {
    const accepted = attempt.current;
    if (!accepted || busyRef.current || !live.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      if (!accepted.bound) {
        setStage("bind");
        accepted.bound = await api.bindPlaybook(
          project,
          accepted.id,
          accepted.inputs,
          accepted.receipt,
          accepted.bindId,
        );
      }
      if (!live.current) return;
      if (!accepted.applied) {
        setStage("apply");
        const result = await api.applyPlaybook(
          project,
          accepted.bound.receipt,
          accepted.applyId,
        );
        accepted.applied = true;
        setStatus(result);
        accepted.inputs = { ...accepted.inputs, bindings: [] };
        setChoices({});
        setReviewed(null);
        setRecovering(false);
      }
      if (!live.current) return;
      setStage("verify");
      const result = await api.verifyPlaybook(project);
      if (live.current) {
        setVerification(result);
        setStage("done");
        setChoices({});
      }
    } catch (e) {
      if (!live.current) return;
      setError(errorText(e));
      try {
        const current = await api.playbookDeployment(project);
        if (live.current) setStatus(current);
      } catch {
        if (live.current) setStatus(null);
      }
    } finally {
      busyRef.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function recoverReview() {
    if (
      !id ||
      !projectId ||
      busyRef.current ||
      !status ||
      status.state === "interrupted"
    )
      return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    setReviewed(null);
    setConfirmed([]);
    try {
      const fresh = await api.playbook(id);
      if (!live.current) return;
      const unavailable = assignmentUnavailable(fresh, false);
      if (unavailable) throw new Error(unavailable);
      const declarations =
        (fresh.documents?.["playbook.toml"] as Manifest | undefined)
          ?.variables ?? [];
      const unchanged = new Set(
        declarations
          .filter((v) =>
            variables.some((old) => old.name === v.name && old.kind === v.kind),
          )
          .map((v) => v.name),
      );
      setDetail(fresh);
      setChoices((all) =>
        Object.fromEntries(
          Object.entries(all).filter(([name]) => unchanged.has(name)),
        ),
      );
      setRecovering(true);
      const next = {
        ...inputs,
        revision: fresh.revision || "",
        bindings: inputs.bindings.filter((v) => unchanged.has(v.name)),
      };
      const plan = await api.reviewPlaybook(id, next);
      if (!live.current) return;
      setReviewed({ inputs: next, plan });
      setRecovering(true);
      attempt.current = null;
    } catch (e) {
      if (live.current) setError(errorText(e));
    } finally {
      busyRef.current = false;
      if (live.current) setBusy(false);
    }
  }
  return {
    library,
    libraryError,
    reloadLibrary,
    id,
    detail,
    loading,
    loadError,
    defaultNotice,
    select: (next: string | null) => {
      manuallySelected.current = true;
      void loadSelection(next);
    },
    variables,
    /** The project exists (a retry after create): an input may then resolve from a saved project
     *  binding the wizard cannot see, so it stops calling inputs missing (#1508). */
    projectExists: !!projectId,
    agents,
    choices,
    setChoices,
    assignments,
    setAssignments,
    trust,
    setTrust,
    reviewed: reviewCurrent ? (reviewed?.plan ?? null) : null,
    confirmed,
    setConfirmed,
    review,
    reviewingCreatedTarget: existingTargetPath === destination,
    selectedReady,
    canCreate,
    busy: busy || removing,
    setRemoving,
    removed: (result: DeploymentStatus) => {
      attempt.current = null;
      setStatus(result);
      setVerification(null);
      setError("");
      setChoices({});
      setReviewed(null);
      setRecovering(false);
      setStage("done");
    },
    error,
    stage,
    status,
    verification,
    prepare,
    deploy,
    recovering,
    recoverReview,
  };
}
