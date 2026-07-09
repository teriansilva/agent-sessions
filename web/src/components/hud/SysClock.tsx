import { useEffect, useState } from "react";

const pad = (n: number) => String(n).padStart(2, "0");
const utc = (d: Date) => `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}Z`;

// Live SYS-UTC readout for the topbar HUD tag (#211 Phase 4). Ticks once a second; the
// interval is cleared on unmount. Purely informational chrome — tabular-nums so it doesn't
// jitter. Tests use fake timers, so the initial render uses the current time directly.
export function SysClock() {
  const [now, setNow] = useState(() => utc(new Date()));
  useEffect(() => {
    const id = setInterval(() => setNow(utc(new Date())), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <span className="hud-tag">
      SYS // <b className="num">{now}</b>
    </span>
  );
}
