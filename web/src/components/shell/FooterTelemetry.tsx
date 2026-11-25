/** The footer's live telemetry (#1531, docs/design.md v2 §5): `CPU 23% ▮▮▯ · RAM 61% 9.8/16 GB ▮▮▮ ·
 *  HQ // CTX 18.2K · TODAY 412K TOK`, polled every 5 s while the tab is visible
 *  (`useTelemetry`). Real numbers only: a host that cannot say shows "—", a failed read says
 *  TELEMETRY // UNAVAILABLE, and an estimated token count carries "≈". No classification text. */
import { useEffect, useRef, useState } from "react";

import { formatTokens, gb, useTelemetry } from "../../lib/useTelemetry";
import { Pips } from "../hud/Meters";
import s from "./FooterTelemetry.module.css";

/** True for one arrival window after `value` changes (never on the first value). */
function useBump(value: unknown): boolean {
  const prev = useRef(value);
  const [bump, setBump] = useState(false);
  useEffect(() => {
    if (Object.is(prev.current, value)) return;
    const first = prev.current === undefined;
    prev.current = value;
    if (first) return;
    setBump(true);
    const t = setTimeout(() => setBump(false), 520);
    return () => clearTimeout(t);
  }, [value]);
  return bump;
}

function Value({ text, testId }: { text: string; testId?: string }) {
  const bump = useBump(text);
  return (
    <b className={`${s.v} ${bump ? s.bump : ""}`.trim()} data-testid={testId}>
      {text}
    </b>
  );
}

const pipsFor = (pct: number) => Math.round((pct / 100) * 8);
const tone = (pct: number) => (pct >= 90 ? "down" : pct >= 75 ? "warn" : undefined);

export function FooterTelemetry() {
  const t = useTelemetry();
  if (t.kind === "unavailable")
    return (
      <span className={s.cluster} data-testid="footer-telemetry" data-state="unavailable">
        <span className={s.stripe} aria-hidden="true" />
        <span className={s.unavailable}>Telemetry // unavailable</span>
      </span>
    );
  const d = t.kind === "ok" ? t.data : null;
  const cpu = d?.cpu_pct ?? null;
  const mem = d?.mem ?? null;
  const hq = d?.hq ?? null;
  return (
    <span
      className={s.cluster}
      data-testid="footer-telemetry"
      data-state={t.kind}
      aria-label="Host and HQ telemetry"
      role="group"
    >
      <span className={s.stripe} aria-hidden="true" />
      <span className={s.tm}>
        CPU <Value text={cpu === null ? "—" : `${Math.round(cpu)}%`} testId="tm-cpu" />
        {cpu !== null && (
          <span className={s.cpuPips}>
            <Pips value={pipsFor(cpu)} max={8} tone={tone(cpu)} label={`CPU ${Math.round(cpu)}%`} />
          </span>
        )}
      </span>
      <span className={s.sep} aria-hidden="true" />
      <span className={s.tm}>
        RAM <Value text={mem === null ? "—" : `${Math.round(mem.pct)}%`} testId="tm-ram" />
        {mem !== null && (
          <>
            <span className={`${s.dim} ${s.wide}`}>
              {gb(mem.used)} / {gb(mem.total)} GB
            </span>
            <span className={s.wide}>
              <Pips value={pipsFor(mem.pct)} max={8} tone={tone(mem.pct)} label={`RAM ${Math.round(mem.pct)}%`} />
            </span>
          </>
        )}
      </span>
      <span className={s.sep} aria-hidden="true" />
      <span className={s.tm}>
        HQ // CTX{" "}
        <Value
          text={
            hq?.ctx_tokens == null
              ? "—"
              : `${hq.ctx_estimated ? "≈" : ""}${formatTokens(hq.ctx_tokens)}`
          }
          testId="tm-ctx"
        />
        <span className={s.wide}>
          · TODAY{" "}
          <Value
            text={hq ? `${hq.today_estimated ? "≈" : ""}${formatTokens(hq.today_tokens)}` : "—"}
            testId="tm-today"
          />{" "}
          TOK
        </span>
      </span>
    </span>
  );
}
