import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import type { AskSubagent } from "../../types/api";

/** Re-read sooner while something is moving: a session working, a spawn queued or starting. */
export const BUSY_POLL_MS = 5_000;
/** Still observed while a session exists at all (Hermes 9131): a waiting, done or taken-over
 *  subagent can run again when its question is answered, HQ steers it, or someone types in it. */
export const IDLE_POLL_MS = 15_000;

// `starting`: its card's claim may still be creating it; `unreadable`: a transient read failure
// (Hermes 9134). Both keep the 5 s poll: only a verified `gone` ends observation of a session.
const BUSY = new Set(["working", "queued", "started", "asks", "starting", "unreadable"]);

function stateOf(r: AskSubagent): string {
  return r.state ?? r.outcome;
}

/** ONE poller per open conversation for every subagent group in it (#2398): the live states
 *  from `GET …/subagents`, re-read when the stored handles change, every `BUSY_POLL_MS` while
 *  one is moving, and every `IDLE_POLL_MS` while any handle still has a session. Nothing is
 *  polled for a conversation whose handles all ended without one. */
export function useLiveSubagents(
  cid: string | null,
  stored: AskSubagent[],
): { rows: AskSubagent[]; refresh: () => void } {
  const [live, setLive] = useState<{ cid: string; rows: AskSubagent[] } | null>(null);
  const [tick, setTick] = useState(0);
  const sig = stored.map((r) => `${r.turn_id}:${r.handle}:${r.outcome}:${r.key ?? ""}`).join(",");
  const any = stored.length > 0;

  useEffect(() => {
    if (!cid || !any) return;
    let current = true;
    api.askSubagents(cid).then(
      (body) => {
        if (current) setLive({ cid, rows: body.subagents });
      },
      () => undefined, // the stored outcomes stay on screen; the next tick tries again
    );
    return () => {
      current = false;
    };
  }, [cid, any, sig, tick]);

  const rows = live && live.cid === cid ? live.rows : stored;
  const busy = rows.some((r) => BUSY.has(stateOf(r)));
  const extant = rows.some((r) => Boolean(r.key) && stateOf(r) !== "gone");
  const every = busy ? BUSY_POLL_MS : extant ? IDLE_POLL_MS : 0;
  useEffect(() => {
    if (!cid || !every) return;
    const t = window.setInterval(() => setTick((n) => n + 1), every);
    return () => window.clearInterval(t);
  }, [cid, every]);

  return { rows: any ? rows : [], refresh: () => setTick((n) => n + 1) };
}
