/** Save as mission plan (#1924): a mission's latest plan revision becomes a reusable, slot-only,
 *  inert plan in the Library. Each project the mission used becomes a slot the operator names; the
 *  project's own name shows here only, as a local hint, and is never saved. */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { missionPlanPath } from "../../lib/routes";
import type { MissionPlanRevision } from "../../types/missionPlans";
import dlg from "../HudDialog.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";
import { useInertBehind } from "../templates/useInertBehind";
import { useHoldFocusWhileBusy } from "../templates/useHoldFocusWhileBusy";
import {
  LABEL_MAX,
  NEVER_SAVED,
  oneLineProblem,
  slotsOf,
  summaryProblem,
  TITLE_MAX,
} from "./planRules";
import styles from "./missionPlans.module.css";

export function SaveAsPlanDialog({
  missionId,
  returnFocusTo,
  onClose,
}: {
  missionId: string;
  returnFocusTo: HTMLElement | null;
  onClose: () => void;
}) {
  const panel = useRef<HTMLDivElement | null>(null);
  const titleInput = useRef<HTMLInputElement | null>(null);
  const [revision, setRevision] = useState<MissionPlanRevision | null>(null);
  const [names, setNames] = useState<Map<string, string>>(new Map());
  const [error, setError] = useState("");
  // Empty, never the mission's title: that can name a project, and a plan's title is exported.
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [labels, setLabels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  useHoldFocusWhileBusy(panel, busy);
  // Focus moves INTO the dialog (the independent review of #1924 S3): the panel at once, the title
  // once the plan is read. A frame later, so a closing menu sheet cannot hand it back to its
  // trigger behind the inert page.
  useEffect(() => {
    const id = requestAnimationFrame(() => panel.current?.focus());
    return () => cancelAnimationFrame(id);
  }, []);
  // …and again once the read ANSWERS — a plan, none, or an error — well after a closing phone
  // sheet's own restore frame (independent review of S3): the title with a plan, else the panel.
  const answered = revision ? (revision.plan ? "plan" : "none") : error ? "error" : null;
  useEffect(() => {
    if (!answered) return;
    const id = requestAnimationFrame(() =>
      (answered === "plan" ? titleInput.current : panel.current)?.focus(),
    );
    return () => cancelAnimationFrame(id);
  }, [answered]);
  useInertBehind(returnFocusTo);
  useFocusContainment({ active: true, panelRef: panel });
  useEffect(() => {
    let live = true;
    void api
      .missionPlanRevision(missionId)
      .then((rev) => {
        if (!live) return;
        setRevision(rev);
        if (rev.plan) setLabels(slotsOf(rev.plan).map((_, i) => `Project ${i + 1}`));
      })
      .catch((e: unknown) => live && setError(e instanceof Error ? e.message : "The plan can’t be read."));
    // Project names are LOCAL hints beside each slot: read, shown, never sent.
    void api
      .projectEntities({ includeArchived: true })
      .then((r) => live && setNames(new Map(r.projects.map((p) => [p.id, p.name]))))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [missionId]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      if (!busy) onClose();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [busy, onClose]);
  const plan = revision?.plan ?? null;
  const slots = plan ? slotsOf(plan) : [];
  const problems = [
    oneLineProblem(title, "The title", TITLE_MAX),
    summaryProblem(summary),
    ...labels.map((l, i) => oneLineProblem(l, `Slot P${i + 1}'s label`, LABEL_MAX)),
  ].filter((p): p is string => !!p);
  const save = async () => {
    if (!plan) return;
    setBusy(true);
    setError("");
    try {
      const out = await api.saveMissionAsPlan(missionId, {
        revision: plan.revision,
        title,
        summary,
        slot_labels: labels,
      });
      setSaved(out.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn’t save the plan.");
    } finally {
      setBusy(false);
    }
  };
  const titleId = "save-as-plan-title";
  return createPortal(
    <div className={dlg.backdrop} onMouseDown={() => (busy ? undefined : onClose())}>
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-busy={busy || undefined}
        tabIndex={-1}
        className={dlg.dialog}
        onMouseDown={(e) => e.stopPropagation()}
        data-testid="save-as-plan-dialog"
      >
        <div className={dlg.head}>
          <span id={titleId} className={dlg.tag}>
            Save as mission plan
          </span>
        </div>
        {!revision && !error && <p role="status">Reading the mission’s plan…</p>}
        {revision && !plan && (
          <p className={dlg.muted} data-testid="save-as-plan-none">
            This mission has no plan document to save yet: apply a procedure to it first.
          </p>
        )}
        {saved ? (
          <p role="status" data-testid="save-as-plan-saved">
            Saved to your Library. <Link to={missionPlanPath(saved)}>Open the mission plan</Link>
          </p>
        ) : (
          plan && (
            <>
              <p className={dlg.muted}>
                Saves plan revision {plan.revision}: this mission’s objectives, their order and the
                procedures they follow, as a plan you can reuse.
              </p>
              <label className={styles.f}>
                <span className={styles.flabel}>Title</span>
                <input
                  ref={titleInput}
                  className={styles.field}
                  placeholder="Name it by what it achieves"
                  value={title}
                  maxLength={TITLE_MAX}
                  onChange={(e) => setTitle(e.target.value)}
                  data-testid="save-as-plan-title-input"
                />
              </label>
              <label className={styles.f}>
                <span className={styles.flabel}>Summary · optional</span>
                <textarea
                  className={styles.field}
                  rows={2}
                  value={summary}
                  onChange={(e) => setSummary(e.target.value)}
                />
              </label>
              <div className={styles.f}>
                <span className={styles.flabel}>Project slots</span>
                <span className={styles.hint}>
                  Each project this mission used becomes a slot. Name it by its role; the project itself
                  is not saved.
                </span>
                {slots.map((s, i) => (
                  <div key={s.id}>
                    <div className={styles.slotRow}>
                      <span className={styles.slotId}>{s.id}</span>
                      <input
                        className={styles.field}
                        aria-label={`Slot ${s.id.toUpperCase()} label`}
                        value={labels[i] ?? ""}
                        maxLength={LABEL_MAX}
                        onChange={(e) =>
                          setLabels((old) => old.map((l, j) => (j === i ? e.target.value : l)))
                        }
                        data-testid={`save-as-plan-slot-${s.id}`}
                      />
                    </div>
                    <div className={`${styles.hint} ${styles.slotHint}`}>
                      now: {names.get(s.projectId) ?? "a project"} · objective{s.objectives.length === 1 ? "" : "s"}{" "}
                      {s.objectives.join(", ")}
                    </div>
                  </div>
                ))}
              </div>
              <div className={styles.f}>
                <span className={styles.flabel}>Never saved in a plan</span>
                <ul className={styles.never}>
                  {NEVER_SAVED.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </div>
            </>
          )
        )}
        {error && (
          <p role="alert" className={styles.error}>
            {error}
          </p>
        )}
        {!saved && plan && problems.length > 0 && (
          <p className={styles.hint} id="save-as-plan-problem" data-testid="save-as-plan-problem">
            {problems[0]}
          </p>
        )}
        <div className={`${dlg.actions} ${styles.stickyActions}`}>
          <button type="button" className={dlg.cancel} disabled={busy} onClick={onClose}>
            {saved || !plan ? "Close" : "Cancel"}
          </button>
          {!saved && plan && (
            <button
              type="button"
              className={dlg.go}
              disabled={busy || problems.length > 0}
              aria-describedby={problems.length > 0 ? "save-as-plan-problem" : undefined}
              onClick={() => void save()}
              data-testid="save-as-plan-save"
            >
              Save to Library
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
