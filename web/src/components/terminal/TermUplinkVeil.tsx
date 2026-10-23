/** The uplink veil (#1570): over the terminal viewport while a session is slow to connect or has
 *  lost its link, past the grace that `useUplinkVeil` owns. The pane behind stays visible but
 *  recedes (blur + a tint toward --ground, which darkens in dark and lightens in light); a framed
 *  uplink log in the boot sequence's idiom says what is happening. Real reads only: the session
 *  title, the elapsed time, the socket's own try count — never invented telemetry. */
import { useEffect, useState } from "react";
import s from "./TermUplinkVeil.module.css";
import type { UplinkVeil } from "./useUplinkVeil";

function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const sec = total % 60;
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

/** What happens to input while the link is down (#1570 follow-up): a terminal refuses keys; an API
 *  session's outbox holds sent messages and posts them, in order, once the link is back. */
export type VeilInput = "keys" | "outbox";

export function TermUplinkVeil({
  veil,
  title,
  input = "keys",
}: {
  veil: UplinkVeil;
  title?: string | null;
  input?: VeilInput;
}) {
  // A 1 s ticker for the elapsed readout, only while an outage is on screen.
  const [now, setNow] = useState(() => Date.now());
  const ticking = veil.phase !== "restored";
  useEffect(() => {
    if (!ticking) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [ticking]);

  const name = title?.trim();
  const head =
    veil.phase === "establishing"
      ? "Uplink // establishing"
      : veil.phase === "lost"
        ? "Uplink // re-establishing"
        : "Uplink // restored";

  return (
    <div
      className={`${s.veil} ${veil.leaving ? s.leaving : "nj-fade-ok"}`}
      data-testid="uplink-veil"
      data-phase={veil.phase}
    >
      <div className={s.log}>
        {/* Only the heading is a live region: a phase change is announced once, while the 1 s
            elapsed counter and the try count stay readable without a per-second announcement. */}
        <div className={s.head} role="status" aria-live="polite" data-testid="uplink-head">
          {veil.phase === "restored" ? <span className={s.ok}>✓</span> : <span className="nj-notch" />}
          {head}
        </div>
        {name && (
          <div className={s.line}>
            <span className={s.key}>Session //</span> <span className={s.val}>{name}</span>
          </div>
        )}
        {veil.phase === "establishing" && (
          <div className={s.line}>
            <span className={s.key}>Waiting //</span> <span className={s.val}>{clock(now - veil.since)}</span>
          </div>
        )}
        {veil.phase === "lost" && (
          <div className={s.line}>
            <span className={s.key}>
              <span className={s.lost}>▲</span> Link lost //
            </span>{" "}
            <span className={s.val} data-testid="uplink-elapsed">
              {clock(now - veil.since)}
            </span>
          </div>
        )}
        {veil.phase === "restored" ? (
          <div className={s.line}>
            <span className={s.key}>
              <span className={s.ok}>✓</span> Down for //
            </span>{" "}
            <span className={s.val}>{clock(veil.downMs ?? 0)}</span>
          </div>
        ) : (
          <div className={s.line}>
            <span className={s.key}>Attempt //</span> <span className={s.val}>{veil.attempt}</span>
          </div>
        )}
        <div className={`${s.bar} ${veil.phase === "restored" ? s.done : ""}`} />
        <div className={s.note}>
          {veil.phase === "restored"
            ? "Input is live again."
            : input === "outbox"
              ? "Messages you send now wait in the outbox and go out when the link is back."
              : veil.phase === "lost"
                ? "Input is paused — keys typed now are not sent."
                : "Input is paused until the link is up."}
        </div>
      </div>
    </div>
  );
}
