/** Read-only graph and equivalent small-screen list, including the saved rework contract. */
import { useState } from "react";
import { useIsMobile } from "../../lib/useIsMobile";
import { PlaybookFlowCanvas } from "./PlaybookFlowCanvas";
import { PlaybookActor } from "./PlaybookCard";
import type { Step } from "./playbookDraft";
import { isNote } from "./playbookGraph";
import buttons from "../ui/actionButton.module.css";
import styles from "./playbooks.module.css";

export function PlaybookFlowPreview({ steps }: { steps: Step[] }) {
  const [view, setView] = useState<"canvas" | "list">("canvas");
  const mobile = useIsMobile();
  const canvas = !mobile && view === "canvas";
  return (
    <>
      {!mobile && (
        <div className={styles.actions} aria-label="Flow preview view">
          <button
            type="button"
            className={canvas ? buttons.primary : buttons.ghost}
            aria-pressed={canvas}
            onClick={() => setView("canvas")}
          >
            Canvas
          </button>
          <button
            type="button"
            className={!canvas ? buttons.primary : buttons.ghost}
            aria-pressed={!canvas}
            onClick={() => setView("list")}
          >
            List
          </button>
        </div>
      )}
      {canvas && <PlaybookFlowCanvas steps={steps} />}
      <ol className={styles.stepList} hidden={canvas}>
        {steps.map((step) => (
          <li key={step.id}>
            <div>
              <strong>{step.title}</strong>
              <PlaybookActor
                step={{
                  ...step,
                  actor: step.actor as Parameters<
                    typeof PlaybookActor
                  >[0]["step"]["actor"],
                  after: step.after ?? [],
                  note: isNote(step),
                }}
              />
            </div>
            <p>
              {isNote(step)
                ? "Note · gates nothing"
                : step.after?.length
                  ? `After: ${step.after.join(", ")}`
                  : "Starting step"}
            </p>
            {step.rework && (
              <p>
                Rework to{" "}
                {steps.find((s) => s.id === step.rework!.to)?.title ??
                  step.rework.to}{" "}
                when {step.rework.when} requests changes · at most{" "}
                {step.rework.max_rounds} rounds.
              </p>
            )}
          </li>
        ))}
      </ol>
    </>
  );
}
