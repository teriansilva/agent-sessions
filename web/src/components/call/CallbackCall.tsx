/** The Call action on a "call me back" notice (#2341 §4): it calls the session straight back,
 *  which answers in its own voice. A notice of another kind has no Call. */
import { Phone } from "lucide-react";

import b from "../ui/actionButton.module.css";
import { callActive, useCall } from "./callContext";
import s from "./call.module.css";

export function CallbackCall({
  sessionKey,
  title,
  onCall,
}: {
  sessionKey: string;
  title: string;
  onCall?: () => void;
}) {
  const { caps, snap, start } = useCall();
  if (!caps?.configured) return null;
  const why = !caps.sessions
    ? caps.session_reason || "calls aren't available right now"
    : callActive(snap)
      ? "a call is already in progress"
      : "";
  return (
    <button
      type="button"
      className={`${b.primary} ${b.compact} ${s.callbackCall}`}
      aria-label={why ? `Call ${title} — unavailable: ${why}` : `Call ${title}`}
      aria-disabled={why ? true : undefined}
      title={why || `Call ${title}`}
      onClick={(e) => {
        if (why) return;
        onCall?.();
        start({ party: sessionKey, trigger: e.currentTarget });
      }}
      data-testid="bell-callback-call"
    >
      <Phone size={14} aria-hidden="true" /> Call
    </button>
  );
}
