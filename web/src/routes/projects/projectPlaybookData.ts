/** What the Playbook tab reads about a project's deployment (#2143 P2, #1949): its status, the
 *  playbook's detail and fleet row (what version is current, whether an update exists), and a
 *  verification. Verify is an on-demand read with no stored time, so the tab runs it when it opens
 *  (on an applied deployment) and on Verify again; `verifiedAt` is that run's local time.
 *
 *  Every read is its own and fails on its own: a failed status read is the tab's red state, a
 *  failed detail / fleet / verify read says so where it would have shown. A late answer from an
 *  earlier read never overwrites a newer one (`epoch`). */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import type { DeploymentStatus } from "../../types/playbookDeployment";
import type { PlaybookDetail, PlaybookVerify } from "../../types/playbooks";
import { readError } from "./projectsData";

export type FleetRow = { revision: string | null; current: string; updateAvailable: boolean };
export type Verification = PlaybookVerify & { update_available?: boolean | null };

export function usePlaybookDeployment(pid: string) {
  const [status, setStatus] = useState<DeploymentStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [detail, setDetail] = useState<PlaybookDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [fleet, setFleet] = useState<FleetRow | null>(null);
  const [verification, setVerification] = useState<Verification | null>(null);
  const [verifiedAt, setVerifiedAt] = useState<Date | null>(null);
  const [verifyError, setVerifyError] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);
  const epoch = useRef(0);
  const verifyEpoch = useRef(0);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const verify = useCallback(async () => {
    const mine = ++verifyEpoch.current;
    setVerifying(true);
    setVerifyError(null);
    try {
      const result = (await api.verifyPlaybook(pid)) as Verification;
      if (!live.current || mine !== verifyEpoch.current) return;
      setVerification(result);
      setVerifiedAt(new Date());
    } catch (e) {
      if (!live.current || mine !== verifyEpoch.current) return;
      setVerifyError(readError(e, "Couldn’t run the verification"));
    } finally {
      if (live.current && mine === verifyEpoch.current) setVerifying(false);
    }
  }, [pid]);

  /** Re-read everything; verify only runs on an applied deployment. `verified` is a verification
   *  the caller just ran itself (the deploy's own last step), so it is shown instead of a second run. */
  const reload = useCallback(async (verified?: PlaybookVerify | null) => {
    const mine = ++epoch.current;
    let next: DeploymentStatus;
    try {
      next = await api.playbookDeployment(pid);
    } catch (e) {
      if (live.current && mine === epoch.current)
        setStatusError(readError(e, "Couldn’t read the project’s playbook"));
      return;
    }
    if (!live.current || mine !== epoch.current) return;
    setStatus(next);
    setStatusError(null);
    const book = next.state === "removed" || next.state === "none" ? undefined : next.playbook_id;
    if (!book) {
      setDetail(null);
      setFleet(null);
      setVerification(null);
      setVerifiedAt(null);
      setVerifyError(null);
      return;
    }
    if (next.state === "applied" && verified) {
      verifyEpoch.current++;
      setVerification(verified);
      setVerifiedAt(new Date());
      setVerifyError(null);
      setVerifying(false);
    } else if (next.state === "applied") void verify();
    else {
      verifyEpoch.current++;
      setVerification(null);
      setVerifiedAt(null);
      setVerifyError(null);
      setVerifying(false);
    }
    const [d, f] = await Promise.allSettled([api.playbook(book), api.playbookProjects(book)]);
    if (!live.current || mine !== epoch.current) return;
    if (d.status === "fulfilled") {
      setDetail(d.value);
      setDetailError(null);
    } else setDetailError(readError(d.reason, "Couldn’t read the playbook"));
    if (f.status === "fulfilled") {
      const row = f.value.projects.find((p) => p.project_id === pid);
      setFleet(
        row
          ? { revision: row.revision, current: f.value.revision, updateAvailable: row.update_available }
          : null,
      );
    } else setFleet(null);
  }, [pid, verify]);

  useEffect(() => {
    // The tab's first read; later reads are the operator's (Retry, Verify again) or follow a write.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
  }, [reload]);

  return {
    status,
    statusError,
    detail,
    detailError,
    fleet,
    verification,
    verifiedAt,
    verifyError,
    verifying,
    verify,
    reload,
  };
}
