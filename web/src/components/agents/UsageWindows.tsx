import type { AgentUsageRow } from "../../types/api";
import { firstToRunOut, resetText, stalenessNote, tone } from "../../lib/agentUsage";
import s from "./UsageWindows.module.css";

/** Every quota window of one account's figures, each with its own meter (#2331).
 *
 *  `full` is the Usage section's list (label, reset, meter, percentage, one row each); `compact`
 *  is an account row's strip of small meters. Both mark the window that runs out first
 *  (`firstToRunOut`). Status colour is the state (docs/design.md §3), never the accent. */
export function UsageWindows({
  row,
  threshold,
  compact = false,
}: {
  row: AgentUsageRow;
  threshold: number;
  compact?: boolean;
}) {
  const windows = row.windows || [];
  const first = windows.length > 1 ? firstToRunOut(row) : null;
  const note = stalenessNote(row);
  if (!windows.length) {
    return (
      <p className={s.none} data-testid="usage-windows-none">
        {row.error
          ? `No figures yet — ${row.error}`
          : row.at
            ? "The agent reported no quota windows."
            : "Not asked yet — Refresh asks every account."}
      </p>
    );
  }
  return (
    <div className={compact ? s.compact : s.full} data-testid="usage-windows">
      {windows.map((w) => {
        const t = tone(w.used_pct, threshold);
        const pct = Math.round(w.used_pct);
        const width = Math.max(0, Math.min(100, w.used_pct));
        const isFirst = first === w.label;
        const reset = resetText(w.resets_at);
        const meter = (
          <span
            className={`${s.meter} ${s[`m_${t}`]}`}
            role="meter"
            aria-valuenow={pct}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={`${w.label} used`}
          >
            <span className={s.fill} style={{ width: `${width}%` }} />
          </span>
        );
        return compact ? (
          <div key={w.label} className={s.mini} data-window={w.label}>
            <span className={s.miniLabel}>
              {w.label} <b className={s[`p_${t}`]}>{pct}%</b>
            </span>
            {meter}
          </div>
        ) : (
          <div
            key={w.label}
            className={`${s.win} ${isFirst ? s.first : ""}`}
            data-window={w.label}
            data-first={isFirst || undefined}
          >
            <span className={s.label}>
              {w.label}
              <small>
                {reset ? `resets ${reset}` : "no reset stated"}
                {isFirst ? " · runs out first" : ""}
              </small>
            </span>
            {meter}
            <span className={`${s.pct} ${s[`p_${t}`]}`}>{pct}%</span>
          </div>
        );
      })}
      {note ? <p className={s.note}>{note}</p> : null}
    </div>
  );
}

/** The Usage section's header: whose figures (the account's own name, then the email the vendor
 *  reports for it), which plan, and whether new sessions start there. Nothing to say → nothing. */
export function UsageAccountHead({ row, pooled }: { row: AgentUsageRow; pooled: boolean }) {
  const acc = row.account;
  // An agent on its only login has no name of its own worth a heading: the email says it.
  const label = acc && (pooled || acc.id !== "default") ? acc.label : "";
  const email = acc?.email && acc.email !== label ? acc.email : "";
  const bits = [email, row.plan ? `${row.plan} plan` : ""].filter(Boolean);
  if (!label && !bits.length) return null;
  return (
    <div className={s.head} data-testid="usage-account">
      <span>
        {label ? <b>{label}</b> : null}
        {bits.length ? (
          <span className={s.plan}>
            {label ? " · " : ""}
            {bits.join(" · ")}
          </span>
        ) : null}
      </span>
      {pooled ? <span className={s.tag}>New sessions start here</span> : null}
    </div>
  );
}
