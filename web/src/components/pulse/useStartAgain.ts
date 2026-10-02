/** Start again: `failed -> planned` for a launch that typed nothing (#966 P2, drawn by #967 P4).
 *
 *  ONE function for both entry points, the header's primary slot and the failure block in the thread,
 *  so they cannot disagree about when it is offered, what it sends, or what a refusal looks like.
 *
 *  - **Offered from the detail.** `canStartAgain` reads `mission.retry_eligible`, which the server
 *    computes from its recorded evidence. The failure event's own `retry_eligible` is a snapshot from
 *    the moment the launch settled and is never consulted.
 *  - **Sent as the ordinary state write.** `{from: "failed", to: "planned"}` through `setMissionState`,
 *    with its CSRF header. The server re-reads the evidence inside that transaction; nothing here is
 *    authority.
 *  - **A refusal is the server's words.** A 409 carries a `detail` ("…cannot be started again: a launch
 *    for this mission is still in flight…"), and `mutateJson` puts it on the `ApiError`. It is kept
 *    here for the failure block to show in place, and forwarded to `onNote` only when the caller says
 *    there is no block on screen to show it.
 *  - **Settlement always re-reads**, success or refusal, and the controls stay disabled until a NEW
 *    detail object arrives: a success whose read has not landed still shows `failed` + eligible, and a
 *    second press there would only 409. The same rule `useMissionStart` applies to Begin.
 */
import { useCallback, useLayoutEffect, useRef, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { Mission } from "../../types/api";

import { canStartAgain } from "./missionThread";

export function useStartAgain(
  mission: Mission | null,
  {
    onChanged,
    onNote,
  }: {
    /** Re-read the detail (and the rail). Called on every settlement. */
    onChanged: () => void;
    /** Where a refusal goes when no failure block is on screen to show it. */
    onNote?: (msg: string) => void;
  },
) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [awaitingRead, setAwaitingRead] = useState<Mission | null>(null);
  const inFlight = useRef(false);
  const latest = useRef(mission);
  useLayoutEffect(() => {
    latest.current = mission;
  }, [mission]);

  const eligible = canStartAgain(mission);
  const waiting = mission !== null && awaitingRead === mission;

  const run = useCallback(async () => {
    if (!mission || !canStartAgain(mission) || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await api.setMissionState(mission.id, { from: "failed", to: "planned" });
    } catch (err) {
      const msg =
        err instanceof ApiError && err.message
          ? err.message
          : "Start again did not work. Check the mission before trying again.";
      setError(msg);
      onNote?.(msg);
    } finally {
      setAwaitingRead(latest.current);
      inFlight.current = false;
      setBusy(false);
      onChanged();
    }
  }, [mission, onChanged, onNote]);

  return {
    /** The detail says Start again is allowed. */
    eligible,
    busy,
    /** Both entry points use this: in flight, or waiting for the read that follows. */
    disabled: busy || waiting,
    /** The last refusal, in the server's words. */
    error,
    /** Why it is not allowed, from the detail. */
    reason: mission?.retry_reason ?? null,
    run,
  };
}

export type StartAgain = ReturnType<typeof useStartAgain>;
