import type { RemoteShare } from "../../lib/federation";
import { LEAVE_LABEL, leaveAria } from "../../lib/federationLeave";
import btn from "../ui/actionButton.module.css";
import styles from "./Remote.module.css";

/** The Leave button itself: a plain ghost button, never a menu item. */
export function LeaveButton({
  share,
  onLeave,
  className,
}: {
  share: RemoteShare;
  onLeave: (share: RemoteShare, opener: HTMLElement) => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      className={`${btn.ghost} ${btn.compact} ${styles.leave} ${className ?? ""}`}
      aria-label={leaveAria(share.title, share.peer_name || "the other instance")}
      data-testid="share-leave"
      onClick={(e) => onLeave(share, e.currentTarget)}
    >
      {LEAVE_LABEL}
    </button>
  );
}
