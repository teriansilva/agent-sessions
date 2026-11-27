/** Call on an API session's composer (#2341): the call goes straight to this session, which
 *  answers in its own voice. It sits before Send; the mic beside it keeps hold-to-dictate (S1).
 *
 *  - Capabilities not read yet, or calls not set up: nothing (the composer never grows a control
 *    that turns into "Set up calls" a moment later).
 *  - Set up but not available now (the voice runtime is missing, or this page rides Home Free):
 *    the button says why and starts nothing.
 *  - On a call with THIS session: "On call", an accent edge (not a status colour), and it opens the
 *    call panel. On a call with anyone else: disabled, and the title says so. */
import { Phone, PhoneCall } from "lucide-react";

import { callActive, useCall } from "./callContext";

export function SessionCallButton({
  sessionKey,
  title,
  className,
  onClassName,
  labelClassName,
  disabled,
}: {
  sessionKey: string;
  /** The session's title, for the accessible name ("Call docs-refresh"). */
  title: string;
  className: string;
  /** Added while this session is on a call. */
  onClassName: string;
  labelClassName?: string;
  disabled?: boolean;
}) {
  const { caps, snap, start, setMinimized } = useCall();
  if (!caps || !caps.configured) return null;
  const who = title || "this session";
  const active = callActive(snap);
  if (active && snap.party?.key === sessionKey) {
    return (
      <button
        type="button"
        className={`${className} ${onClassName}`}
        aria-label={`On a call with ${who} — open the call`}
        title={`On a call with ${who}`}
        onClick={() => setMinimized(false)}
        data-testid="structured-call"
        data-call="active"
      >
        <PhoneCall size={16} aria-hidden="true" />
        <span className={labelClassName}>On call</span>
      </button>
    );
  }
  const busy = active;
  const why = !caps.sessions
    ? caps.session_reason || "calls aren't available right now"
    : busy
      ? "a call is already in progress"
      : "";
  const off = !!why || !!disabled;
  return (
    <button
      type="button"
      className={className}
      aria-label={why ? `Call ${who} — unavailable: ${why}` : `Call ${who}`}
      aria-disabled={off ? true : undefined}
      title={why || `Call ${who}: it answers in its own voice`}
      onClick={(e) => {
        if (off) return;
        start({ party: sessionKey, trigger: e.currentTarget });
      }}
      data-testid="structured-call"
      data-call={why ? "unavailable" : "ready"}
    >
      <Phone size={16} aria-hidden="true" />
      <span className={labelClassName}>Call</span>
    </button>
  );
}
