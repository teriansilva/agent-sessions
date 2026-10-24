/** The Call entry points (#1397 P1): ☎ in the top bar and in HQ's header (`variant="icon"`), and
 *  Call under the dashboard's primary-contact card (`variant="card"`).
 *
 *  - Capabilities not read yet: nothing.
 *  - Calls not set up: the control is a link to Settings → HQ calls ("Set up calls").
 *  - Set up but not available now (the voice runtime is missing, HQ's model is not configured, or
 *    this page rides Home Free): the control says why and starts nothing.
 *  - A call in progress: the control opens it (the panel, out of the on-call bar). */
import { Phone, PhoneCall, Settings2 } from "lucide-react";
import { Link } from "react-router-dom";

import { settingsPath } from "../../routes/settingsTabs";
import b from "../ui/actionButton.module.css";
import bell from "../pulse/NotificationBell.module.css";
import { callActive, useCall } from "./callContext";
import s from "./call.module.css";

export function CallButton(props: {
  variant: "icon" | "card";
  conversationId?: string | null;
  testId: string;
  wideOnly?: boolean;
  beforeStart?: () => HTMLElement | null | undefined;
}) {
  return props.wideOnly ? (
    <span className={s.wideOnly}>
      <CallControl {...props} />
    </span>
  ) : (
    <CallControl {...props} />
  );
}

function CallControl({
  variant,
  conversationId,
  testId,
  beforeStart,
}: {
  variant: "icon" | "card";
  /** HQ's header calls into the conversation it shows. */
  conversationId?: string | null;
  testId: string;
  /** The top bar's copy hides at ≤880px (the bar has no width to spare there). */
  wideOnly?: boolean;
  /** Runs at the tap, before the call starts or its panel reopens (HQ's phone drawer closes so the
   *  call panel owns focus); on a start, an element it returns gets focus back when the call
   *  closes, instead of this button. */
  beforeStart?: () => HTMLElement | null | undefined;
}) {
  const { caps, snap, start, setMinimized, minimized } = useCall();
  const active = callActive(snap) || snap.phase === "ended";
  if (active) {
    // Reopening the call steps out of HQ's phone drawer too, as starting one does (#1640 second
    // review): the panel must not open over a drawer whose focus trap is still live.
    const open = () => {
      beforeStart?.();
      setMinimized(false);
    };
    return variant === "icon" ? (
      <button
        type="button"
        className={`${bell.btn} ${bell.btnOn}`}
        aria-label={minimized ? "Open the call with HQ" : "On a call with HQ"}
        title="On a call with HQ"
        onClick={open}
        data-testid={testId}
        data-call="active"
      >
        <PhoneCall size={18} aria-hidden="true" className={s.onCall} />
      </button>
    ) : (
      <div className={s.callCard}>
        <button
          type="button"
          className={`${b.primary} ${s.cardBtn}`}
          onClick={open}
          data-testid={testId}
          data-call="active"
        >
          <PhoneCall size={16} aria-hidden="true" /> On call
        </button>
      </div>
    );
  }

  // Nothing until the server has said whether calls exist here: no Call that turns into "Set up
  // calls" a moment later (and none at all where the shell has no call provider).
  if (!caps) return null;
  if (!caps.configured) {
    return variant === "icon" ? (
      <Link
        className={bell.btn}
        to={settingsPath("ai-hq-calls")}
        aria-label="Set up calls to HQ"
        title="Set up calls to HQ"
        data-testid={testId}
        data-call="setup"
      >
        <Phone size={18} aria-hidden="true" />
      </Link>
    ) : (
      <div className={s.callCard}>
        <Link
          className={`${b.ghost} ${s.cardBtn}`}
          to={settingsPath("ai-hq-calls")}
          data-testid={testId}
          data-call="setup"
        >
          <Settings2 size={16} aria-hidden="true" /> Set up calls
        </Link>
      </div>
    );
  }

  const unavailable = !caps.available;
  const why = unavailable ? caps.reason : "";
  const go = (e: React.MouseEvent<HTMLElement>) => {
    if (unavailable) return;
    const back = beforeStart?.();
    start({ conversationId: conversationId ?? null, trigger: back ?? e.currentTarget });
  };
  return variant === "icon" ? (
    <button
      type="button"
      className={bell.btn}
      aria-label={unavailable ? `Call HQ — unavailable: ${why}` : "Call HQ"}
      aria-disabled={unavailable ? true : undefined}
      title={unavailable ? why : "Call HQ"}
      onClick={go}
      data-testid={testId}
      data-call={unavailable ? "unavailable" : "ready"}
    >
      <Phone size={18} aria-hidden="true" />
    </button>
  ) : (
    <div className={s.callCard}>
      <button
        type="button"
        className={`${unavailable ? b.ghost : b.primary} ${s.cardBtn}`}
        aria-disabled={unavailable ? true : undefined}
        aria-describedby={unavailable ? `${testId}-why` : undefined}
        onClick={go}
        data-testid={testId}
        data-call={unavailable ? "unavailable" : "ready"}
      >
        <Phone size={16} aria-hidden="true" /> Call
      </button>
      {unavailable ? (
        <p className={s.why} id={`${testId}-why`} data-testid={`${testId}-why`}>
          {why}
        </p>
      ) : null}
    </div>
  );
}
