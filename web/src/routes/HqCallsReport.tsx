/** A speech compatibility report (#1397): **Test both**, and the check **Use this machine** runs. */
import type { ConformanceReport } from "../types/voice";
import h from "./HqCallsSettings.module.css";

export function ConformanceReportView({ report, testId = "hq-calls-report" }: { report: ConformanceReport; testId?: string }) {
  return (
    <div className={h.report} data-testid={testId} data-usable={report.usable}>
      <p className={h.reportHead}>
        <span className={`nj-stamp ${report.usable ? "is-ok" : "is-down"}`}>
          {report.usable ? "Usable" : "Not usable"}
        </span>
      </p>
      <ul className={h.checks}>
        {report.checks.map((c) => (
          <li key={c.id} data-testid="hq-calls-check" data-ok={c.ok} data-required={c.required}>
            <span className={`hud-led ${c.ok ? "up" : c.required ? "down" : "att"}`} aria-hidden="true" />
            <span className={h.checkId}>{c.id}</span>
            <span className={h.checkWord}>{c.ok ? "OK" : c.required ? "Failed" : "Missing"}</span>
            {c.note ? <span className={h.checkNote}>{c.note}</span> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
