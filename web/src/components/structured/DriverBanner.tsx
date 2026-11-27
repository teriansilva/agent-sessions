/** Who drives an API session (#2397): HQ (from one of its conversations), you, or a mission.
 *  The chip sits in the session's header; the bar above the composer says what typing does and
 *  offers Hand back to HQ. Server facts only (`snapshot.driver`); the words are authored here. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { missionLink } from "../../lib/missionLink";
import { hqConversationPath } from "../../lib/routes";
import type { StructuredDriver } from "../../types/api";
import s from "./DriverBanner.module.css";

const short = (t: string, n = 28) => (t.length > n ? `${t.slice(0, n - 1)}…` : t);

export function DriverChip({ driver }: { driver?: StructuredDriver }) {
  if (!driver || driver.who === "unknown") return null;
  if (driver.who === "hq") {
    const title = driver.conversation?.title || "an HQ conversation";
    const label = `HQ driving · “${short(title)}”`;
    const body = (
      <>
        <span className={s.led} aria-hidden="true" />
        {label}
      </>
    );
    return driver.conversation ? (
      <Link
        to={hqConversationPath(driver.conversation.id)}
        className={`${s.chip} ${s.hq}`}
        data-testid="structured-driver"
        data-who="hq"
        title={`HQ drives this session from “${title}”. Open that conversation.`}
      >
        {body}
      </Link>
    ) : (
      <span className={`${s.chip} ${s.hq}`} data-testid="structured-driver" data-who="hq">
        {body}
      </span>
    );
  }
  if (driver.who === "mission") {
    const title = driver.mission?.title || "a mission";
    return driver.mission?.id ? (
      <Link
        to={missionLink(driver.mission.id)}
        className={`${s.chip} ${s.mission}`}
        data-testid="structured-driver"
        data-who="mission"
        title={`The mission “${title}” holds this session`}
      >
        <span className={s.led} aria-hidden="true" />
        Mission · {short(title)}
      </Link>
    ) : (
      <span className={`${s.chip} ${s.mission}`} data-testid="structured-driver" data-who="mission">
        <span className={s.led} aria-hidden="true" />
        Mission · {short(title)}
      </span>
    );
  }
  return (
    <span className={s.chip} data-testid="structured-driver" data-who="commander">
      <span className={s.led} aria-hidden="true" />
      You&rsquo;re driving
    </span>
  );
}

const clock = (ts?: number | null) =>
  ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";

/** The line above the composer. ``onChanged`` re-reads the snapshot (after a Hand back, and after
 *  a stale one: the 409 means the driver moved, so the view reloads). */
export function DriverBar({
  sessionKey,
  driver,
  onChanged,
}: {
  sessionKey: string;
  driver?: StructuredDriver;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!driver || driver.who === "unknown") return null;
  const handBack = async () => {
    if (busy || driver.generation == null) return;
    setBusy(true);
    setError(null);
    try {
      await api.handBackDriver(sessionKey, driver.generation);
    } catch (e) {
      const status = (e as { status?: number }).status;
      setError(
        status === 409
          ? "Who drives this session changed meanwhile, so nothing was handed back. Here it is now."
          : `Not handed back: ${(e as Error).message || "refused"}.`,
      );
    } finally {
      setBusy(false);
      onChanged();
    }
  };
  let text: string;
  if (driver.who === "hq") {
    text = "HQ is steering this session. Typing below takes it over; HQ stops sending.";
  } else if (driver.who === "mission") {
    text = "A mission holds this session. HQ never steers it; the mission’s own controls apply.";
  } else if (driver.can_hand_back) {
    const at = clock(driver.since);
    text = `You took over${at ? ` at ${at}` : ""}. HQ reads it but does not send.`;
  } else {
    text = "You’re driving. Ask HQ to take it over.";
  }
  return (
    <div className={s.bar} data-testid="structured-driver-bar" data-who={driver.who} role="note">
      <span>{text}</span>
      {driver.who === "commander" && driver.can_hand_back ? (
        <button
          type="button"
          className={s.handBack}
          disabled={busy}
          onClick={() => void handBack()}
          data-testid="structured-hand-back"
        >
          Hand back to HQ
        </button>
      ) : null}
      {error ? (
        <span className={s.error} role="status" data-testid="structured-hand-back-error">
          {error}
        </span>
      ) : null}
    </div>
  );
}
