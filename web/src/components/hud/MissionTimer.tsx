import { useEffect, useState } from "react";

// "MISSION // T+ Nd HH:MM:SS" elapsed readout for the topbar HUD (#211). Decorative tactical
// chrome — counts up from a fixed mission anchor (the BattleLab rebrand cut-over) so it reads
// as a steady mission clock rather than resetting per page load. Ticks once a second; the
// interval is cleared on unmount; tabular-nums so it doesn't jitter.
const MISSION_ANCHOR = Date.UTC(2026, 4, 26, 8, 20, 11); // 2026-05-26 — the rebrand cut-over

const pad = (n: number) => String(n).padStart(2, "0");

function elapsed(): string {
  let s = Math.max(0, Math.floor((Date.now() - MISSION_ANCHOR) / 1000));
  const d = Math.floor(s / 86400);
  s -= d * 86400;
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  s -= m * 60;
  return `T+ ${d}d ${pad(h)}:${pad(m)}:${pad(s)}`;
}

export function MissionTimer() {
  const [t, setT] = useState(() => elapsed());
  useEffect(() => {
    const id = setInterval(() => setT(elapsed()), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <span className="hud-tag">
      MISSION // <b className="num">{t}</b>
    </span>
  );
}
