/** What HQ is doing, and what it did (#2076): one collapsed row per turn that opens to its steps.
 *
 *  ONE `<details>` — the API pane's activity pattern (`StructuredPane`). While the turn runs its
 *  summary carries the acquire bar, the current step in the operator's words (`askStep.ts`), the
 *  steps so far and the time; settled it reads "23 steps · 1 m 48 s", stopped "Stopped after 9
 *  steps". Opened, it lists every stored step: what it was, what it was about, what came back and
 *  how long it took. The list never scrolls on its own — the thread does.
 *
 *  It shows only what the server stores of a step: the tool's name, its guarded arguments and
 *  numbers. `role="status"` is on the step words alone, so a screen reader hears a step change
 *  without the ticking seconds, and nothing moves outside Full motion (AskConsole.module.css).
 */
import { ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";

import a from "./AskConsole.module.css";
import {
  formatElapsed,
  formatStepTime,
  moreLines,
  processTally,
  stepArgument,
  stepOrdinal,
  stepResult,
  stoppedLine,
  type AskProcessState,
} from "./askProcess";
import { askStepLabel, toolLabel, type AskStep } from "./askStep";

/** This page's clock, once a second, only while something is running. */
function useNow(ticking: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [ticking]);
  return now;
}

export function AskProcess({
  process,
  running,
  stopped = false,
  stopping = false,
  step,
  titles,
}: {
  process: AskProcessState;
  /** The turn is still running on the server. */
  running: boolean;
  /** The operator stopped it: the row opens, and says so. */
  stopped?: boolean;
  /** A Stop is in and the turn has not settled yet. */
  stopping?: boolean;
  /** The step the turn is on (running), from its progress events. */
  step: AskStep | null;
  /** Session keys and mission ids this conversation can name. */
  titles?: ReadonlyMap<string, string>;
}) {
  const now = useNow(running);
  // A stopped turn opens, so what was done is in view; the operator's own toggle wins after.
  const [open, setOpen] = useState(stopped);
  const [wasStopped, setWasStopped] = useState(stopped);
  if (stopped !== wasStopped) {
    setWasStopped(stopped);
    if (stopped) setOpen(true);
  }
  const elapsed =
    process.elapsedMs === null
      ? null
      : process.elapsedMs + (running ? Math.max(0, now - process.elapsedAt) : 0);
  const live = running ? process.live : null;
  // The newest progress line names the step (a search's own sub-steps included); a running turn
  // read back from the server has only the step the server says is live.
  const words = stopping
    ? "Stopping…"
    : step
      ? askStepLabel(step)
      : live
        ? askStepLabel({ step: "tool", tool: live.tool })
        : askStepLabel(null);
  const more = !process.incomplete && process.more && process.more.count > 0 ? process.more : null;
  const tally = running
    ? `${process.count.toLocaleString("en-US")} ${process.count === 1 ? "step" : "steps"}`
    : processTally(process, stopped);
  return (
    <>
      <details
        className={`${a.proc} ${running ? a.procLive : ""}`}
        open={open}
        onToggle={(e) => setOpen(e.currentTarget.open)}
        data-testid={running ? "ask-working" : "ask-process"}
        data-state={running ? "running" : stopped ? "stopped" : "settled"}
      >
        <summary className={a.procSummary} data-testid="ask-process-summary">
          {/* The shared acquire bar (components/hud/nightjar.css), on the row's top edge and
              INSIDE the summary so it shows collapsed. It holds still outside Full motion. */}
          {running ? <div className={`nj-acquire ${a.procBar}`} aria-hidden="true" /> : null}
          <ChevronRight size={16} className={a.procChev} aria-hidden="true" />
          {running ? (
            <>
              <span role="status" className={a.procNow} data-testid="ask-step">
                {words}
              </span>
              <span className={a.procTally} aria-hidden="true" data-testid="ask-process-tally">
                {tally}
                {elapsed !== null ? ` · ${formatElapsed(elapsed)}` : ""}
              </span>
            </>
          ) : stopped || process.incomplete ? (
            <>
              <span className={a.procNow} data-testid="ask-process-tally">
                {tally}
              </span>
              <span className={a.procTally}>{elapsed !== null ? formatElapsed(elapsed) : ""}</span>
            </>
          ) : (
            <span className={`${a.procNow} ${a.procSettled}`} data-testid="ask-process-tally">
              {tally}
              {elapsed !== null ? ` · ${formatElapsed(elapsed)}` : ""}
            </span>
          )}
        </summary>
        {process.steps.length > 0 || live ? (
          <ol className={a.procList} data-testid="ask-process-steps">
            {process.steps.map((s, i) => {
              const result = stepResult(s);
              return (
                <li key={`${s.n ?? i}`} className={a.procStep} data-testid="ask-process-step">
                  <span className={a.procN}>{stepOrdinal(s, i)}</span>
                  <span className={a.procWhat}>
                    <span className={a.procLabel}>{toolLabel(s.name)}</span>
                    <span className={a.procArg}>{stepArgument(s, titles)}</span>
                  </span>
                  <span
                    className={`${a.procRes} ${
                      result.tone === "refused"
                        ? a.procRefused
                        : result.tone === "unknown"
                          ? a.procUnknown
                          : ""
                    }`}
                    data-tone={result.tone}
                  >
                    {result.text}
                  </span>
                  <span className={a.procDur}>
                    {typeof s.ms === "number" ? formatStepTime(s.ms) : ""}
                  </span>
                </li>
              );
            })}
            {live ? (
              <li
                className={`${a.procStep} ${a.procCur}`}
                data-testid="ask-process-live"
                aria-current="step"
              >
                <span className={a.procN}>{String(live.n).padStart(2, "0")}</span>
                <span className={a.procWhat}>
                  <span className={a.procLabel}>{toolLabel(live.tool)}</span>
                  <span className={a.procArg} />
                </span>
                <span className={a.procRes}>
                  <span className={`hud-led ${a.procLed}`} aria-hidden="true" />
                  Running
                </span>
                <span className={a.procDur} aria-hidden="true">
                  {formatElapsed(Math.max(0, now - live.at))}
                </span>
              </li>
            ) : null}
          </ol>
        ) : null}
        {more ? (
          <div className={a.procMore} data-testid="ask-process-more">
            <span>{moreLines(more).head}</span>
            {moreLines(more).byTool ? (
              <p className={a.procByTool}>{moreLines(more).byTool}</p>
            ) : null}
          </div>
        ) : null}
      </details>
      {stopped ? (
        <p className={a.procQuiet} data-testid="ask-stopped-line">
          {stoppedLine(process.steps)}
        </p>
      ) : null}
    </>
  );
}
