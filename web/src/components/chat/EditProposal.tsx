import { useRef, useState } from "react";
import { api } from "../../lib/api";
import type { ChatProposal } from "../../types/api";
import styles from "./EditProposal.module.css";

const LABELS: Record<ChatProposal["status"], string> = {
  awaiting_approval: "Awaiting your approval",
  deciding: "Checking the save outcome",
  approved: "Approved · saved",
  rejected: "Rejected · not saved",
  refused: "Refused · not saved",
  interrupted: "Interrupted · inspect the file",
};

export function EditProposal({ sid, proposal, reload }: {
  sid: string; proposal: ChatProposal; reload: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const heading = useRef<HTMLDivElement>(null);
  const waiting = proposal.status === "awaiting_approval";
  async function decide(decision: "approve" | "reject") {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.chatDecide(sid, proposal.turn_id, proposal.id, decision);
      await reload();
      heading.current?.focus();
    } catch {
      // A lost response is ambiguous. Reload the authoritative decision; the same proposal id
      // makes a deliberate retry safe, but never automatically replay a request.
      try { await reload(); } catch {
        setError("Couldn’t confirm the decision. Check the status before trying again.");
      }
    } finally {
      setBusy(false);
    }
  }
  return <section className={styles.proposal} data-testid="chat-proposal" aria-label={`File change: ${proposal.path}`}>
    <div className={styles.head} ref={heading} tabIndex={-1}>
      <div className={styles.status} role="status">{LABELS[proposal.status]}</div>
      <h3>{waiting ? "Review this file change" : "File change"}</h3>
      <code className={styles.path}>{proposal.path}</code>
      {proposal.decided_by && <p className={styles.note}>Decision by {proposal.decided_by}</p>}
    </div>
    {proposal.reason && <p className={styles.reason}>{proposal.reason}{waiting && " · Reject this proposal and request a fresh read."}</p>}
    {proposal.diff && <pre className={styles.diff} aria-label="Proposed changes">{proposal.diff.split("\n").map((line, i) =>
      <span key={i} className={line.startsWith("+") ? styles.add : line.startsWith("-") ? styles.del : undefined}>{line}{"\n"}</span>,
    )}</pre>}
    {waiting && <div className={styles.actions}>
      <p>This approves only the change shown above. The agent continues after your decision.</p>
      <button type="button" disabled={busy} onClick={() => void decide("reject")}>Reject</button>
      <button type="button" className={styles.primary} disabled={busy || !proposal.can_approve} onClick={() => void decide("approve")}>Approve &amp; save</button>
    </div>}
    {error && <p className={styles.reason} role="alert">{error}</p>}
  </section>;
}
