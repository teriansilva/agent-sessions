/** A messenger connector's notify route (#1841, #1746 P3b): a short copy of each bell event to
 *  ONE destination of the Commander's own, and the route's recent deliveries.
 *
 *  **The destination is attested, never typed.** It is chosen from the saved recipients the
 *  server already bound to this bot (no second destination model), and the server pins its
 *  identity when notify is switched on: a Telegram private chat's user id, the one other person in
 *  a Matrix room of just the bot and them, or the typed address. The page shows that identity and
 *  says what BattleLab can and can't verify. Every refusal is the server's own words.
 *
 *  **Nothing here changes the bell or the push.** A skipped notification (quiet hours, over the
 *  hourly cap) stays in the bell and is never sent later; the deliveries list says so. */
import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../lib/api";
import {
  NOTIFY_CLASSES,
  NOTIFY_CLASS_LABEL,
  NOTIFY_WINDOWS,
  deliveryWords,
  notifyEligible,
  notifyOffLine,
  type ConnectorDelivery,
  type ConnectorInstance,
  type NotifyClass,
} from "../lib/connectors";

import styles from "./ConnectorsSettings.module.css";
import settings from "./Settings.module.css";

function message(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function windowLabel(s: number): string {
  if (s === 0) return "No bundling: one message per event";
  return s < 60 ? `${s} seconds` : `${s / 60} minute${s === 60 ? "" : "s"}`;
}

function when(ts: number): string {
  const d = new Date(ts * 1000);
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return time;
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `Yesterday ${time}`;
  return d.toLocaleDateString([], { day: "numeric", month: "short" });
}

export function ConnectorNotify({
  inst,
  adapter,
  connector,
  busy,
  onChanged,
}: {
  inst: ConnectorInstance;
  adapter: string;
  /** The service's label ("Telegram"), for the disclosure. */
  connector: string;
  busy: boolean;
  onChanged: () => Promise<void>;
}) {
  const n = inst.notify;
  const eligible = (inst.recipients ?? []).filter((r) =>
    notifyEligible(adapter, r.kind),
  );
  const [enabled, setEnabled] = useState(!!n?.enabled);
  const [dest, setDest] = useState(n?.destination || eligible[0]?.id || "");
  const [classes, setClasses] = useState<Set<NotifyClass>>(
    () => new Set(n?.classes ?? NOTIFY_CLASSES),
  );
  const [windowS, setWindowS] = useState(n?.window_s ?? 60);
  const [quietOn, setQuietOn] = useState(!!n?.quiet);
  const [from, setFrom] = useState(n?.quiet?.from ?? "23:00");
  const [to, setTo] = useState(n?.quiet?.to ?? "07:00");
  const tz = n?.quiet?.tz ?? browserZone();
  const [cap, setCap] = useState(String(n?.cap_per_hour ?? 20));
  const [includeLine, setIncludeLine] = useState(!!n?.include_line);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recent, setRecent] = useState<ConnectorDelivery[] | null>(null);
  const [recentError, setRecentError] = useState<string | null>(null);

  const loadRecent = useCallback(async () => {
    try {
      const r = await api.connectorDeliveries(inst.id);
      setRecent(r.deliveries);
      setRecentError(null);
    } catch (e) {
      setRecentError(message(e, "Couldn’t read the recent notifications."));
    }
  }, [inst.id]);

  useEffect(() => {
    let alive = true;
    api
      .connectorDeliveries(inst.id)
      .then((r) => {
        if (alive) setRecent(r.deliveries);
      })
      .catch((e: unknown) => {
        if (alive)
          setRecentError(message(e, "Couldn’t read the recent notifications."));
      });
    return () => {
      alive = false;
    };
  }, [inst.id]);

  // The server's answer is the state: the card remounts this section (its key) when the route's
  // generation or switch changes, so a save or a change elsewhere is shown as the server says.

  if (!n) return null;
  const email = adapter === "smtp";
  const matrix = adapter === "matrix-cs";
  const chosen = eligible.find((r) => r.id === dest);
  const pinned =
    n.identity && n.identity.id === dest && n.enabled ? n.identity : null;
  const capN = Number(cap);
  const capOk = Number.isInteger(capN) && capN >= 1 && capN <= 60;
  const canSwitchOn = !!n.enabled || (inst.notify_ok ?? false);

  const save = async () => {
    setWorking(true);
    setError(null);
    try {
      await api.setConnectorNotify(inst.id, {
        enabled,
        destination_id: dest,
        classes: NOTIFY_CLASSES.filter((c) => classes.has(c)),
        window_s: windowS,
        quiet: quietOn ? { from, to, tz } : null,
        cap_per_hour: capN,
        include_line: includeLine,
      });
      await onChanged();
      await loadRecent();
    } catch (e) {
      setError(message(e, "Couldn’t save the notifications."));
    } finally {
      setWorking(false);
    }
  };

  const identityLine = (): string => {
    if (email)
      return `Mail to ${dest}. BattleLab can’t verify who reads that address, and mail can’t be taken back once it’s sent.`;
    if (pinned && matrix)
      return `Matrix room “${pinned.label}”: just the bot and ${pinned.member}. Only switch this on if ${pinned.member} is you. If anyone else joins, nothing more is sent.`;
    if (pinned)
      return `Telegram user ${pinned.label} (id ${pinned.id}). Only switch this on if that’s you: every notification will go to this chat.`;
    if (matrix)
      return "When you switch this on, BattleLab checks that the room holds just the bot and one person, and shows who that is.";
    return chosen
      ? `Telegram user ${chosen.label} (id ${chosen.id}). Only switch this on if that’s you: every notification will go to this chat.`
      : "";
  };

  const off = !n.enabled ? notifyOffLine(n.off) : null;
  const toggle = (c: NotifyClass, on: boolean) =>
    setClasses((prev) => {
      const next = new Set(prev);
      if (on) next.add(c);
      else next.delete(c);
      return next;
    });
  const idle = !working && !busy;

  return (
    <div className={styles.read} data-testid="connector-notify">
      <h4 className={settings.subhead}>Notifications</h4>
      <p className={settings.hint}>
        Send a short copy of BattleLab’s notifications to{" "}
        {email
          ? "an address of your own"
          : matrix
            ? "a room of just you and the bot"
            : "one private chat you own"}
        . The bell and your device notifications don’t change.
      </p>
      {off && (
        <p
          className={`${settings.callout} ${settings.calloutWarn}`}
          role="note"
          data-testid="connector-notify-off"
        >
          {off}
        </p>
      )}
      {!canSwitchOn && inst.notify_why && (
        <p className={settings.hint} data-testid="connector-notify-why">
          {inst.notify_why}
        </p>
      )}
      <label className={settings.aiToggle}>
        <input
          type="checkbox"
          checked={enabled}
          disabled={!idle || (!canSwitchOn && !enabled)}
          onChange={(e) => setEnabled(e.target.checked)}
          data-testid="connector-notify-enabled"
        />
        <span>Send notifications here</span>
      </label>
      <div className={styles.notifyRow}>
        <label htmlFor={`${inst.id}-notify-dest`}>
          Send to (one of your saved recipients)
        </label>
        <select
          id={`${inst.id}-notify-dest`}
          className={settings.aiInput}
          value={dest}
          disabled={!idle || eligible.length === 0}
          onChange={(e) => setDest(e.target.value)}
          data-testid="connector-notify-destination"
        >
          {eligible.length === 0 && (
            <option value="">No eligible recipient</option>
          )}
          {eligible.map((r) => (
            <option key={r.id} value={r.id}>
              {r.label} · {r.kind === "email" ? "address" : r.kind}
            </option>
          ))}
        </select>
      </div>
      {dest && (
        <p className={settings.hint} data-testid="connector-notify-identity">
          {identityLine()}
        </p>
      )}
      <fieldset className={styles.notifySet}>
        <legend className={settings.hint}>Which notifications</legend>
        {NOTIFY_CLASSES.map((c) => (
          <label key={c} className={settings.aiToggle}>
            <input
              type="checkbox"
              checked={classes.has(c)}
              disabled={!idle}
              onChange={(e) => toggle(c, e.target.checked)}
            />
            <span>{NOTIFY_CLASS_LABEL[c]}</span>
          </label>
        ))}
      </fieldset>
      <div className={styles.notifyRow}>
        <label htmlFor={`${inst.id}-notify-window`}>Bundle events within</label>
        <select
          id={`${inst.id}-notify-window`}
          className={settings.aiInput}
          value={windowS}
          disabled={!idle}
          onChange={(e) => setWindowS(Number(e.target.value))}
        >
          {NOTIFY_WINDOWS.map((w) => (
            <option key={w} value={w}>
              {windowLabel(w)}
            </option>
          ))}
        </select>
      </div>
      <div className={styles.notifyRow}>
        <label className={settings.aiToggle}>
          <input
            type="checkbox"
            checked={quietOn}
            disabled={!idle}
            onChange={(e) => setQuietOn(e.target.checked)}
            data-testid="connector-notify-quiet"
          />
          <span>Quiet hours</span>
        </label>
        <input
          type="time"
          className={`${settings.aiInput} ${styles.notifyTime}`}
          value={from}
          disabled={!idle || !quietOn}
          onChange={(e) => setFrom(e.target.value)}
          aria-label="Quiet hours start"
        />
        <span>to</span>
        <input
          type="time"
          className={`${settings.aiInput} ${styles.notifyTime}`}
          value={to}
          disabled={!idle || !quietOn}
          onChange={(e) => setTo(e.target.value)}
          aria-label="Quiet hours end"
        />
        <span className={styles.meta}>{tz}</span>
      </div>
      <div className={styles.notifyRow}>
        <label htmlFor={`${inst.id}-notify-cap`}>At most</label>
        <input
          id={`${inst.id}-notify-cap`}
          type="number"
          min={1}
          max={60}
          inputMode="numeric"
          className={`${settings.aiInput} ${styles.notifyCap}`}
          value={cap}
          disabled={!idle}
          onChange={(e) => setCap(e.target.value)}
          data-testid="connector-notify-cap"
        />
        <span>messages an hour</span>
      </div>
      <label className={settings.aiToggle}>
        <input
          type="checkbox"
          checked={includeLine}
          disabled={!idle}
          onChange={(e) => setIncludeLine(e.target.checked)}
          data-testid="connector-notify-line"
        />
        <span>Include HQ’s line</span>
      </label>
      <p className={settings.hint}>
        Off: each message holds only what happened (“Waiting for your approval”)
        and a link into BattleLab. On: HQ’s one-line note goes too. It is
        written by your AI from project, mission and automation names, and it
        leaves BattleLab for {connector}.
      </p>
      <p className={settings.callout}>
        Skipped notifications (quiet hours, over the hourly limit) stay in the
        bell; they are never sent later.{" "}
        {email
          ? "Mail can’t be taken back: when you handle one in the app, the mail stays as it was."
          : "When you handle one in the app, its message is edited to “✓ resolved”."}
      </p>
      {error && (
        <p
          className={settings.err}
          role="alert"
          data-testid="connector-notify-error"
        >
          {error}
        </p>
      )}
      <div className={styles.actions}>
        <button
          type="button"
          className={settings.secBtn}
          disabled={
            !idle || !capOk || (enabled && (!dest || classes.size === 0))
          }
          onClick={() => void save()}
          data-testid="connector-notify-save"
        >
          {working ? "Saving…" : "Save notifications"}
        </button>
      </div>
      <h4 className={settings.subhead}>Recent</h4>
      {recentError ? (
        <p
          className={settings.hint}
          data-testid="connector-notify-recent-error"
        >
          {recentError}
        </p>
      ) : recent && recent.length === 0 ? (
        <p className={settings.hint}>Nothing sent yet.</p>
      ) : (
        <ul className={styles.deliveries} data-testid="connector-notify-recent">
          {(recent ?? []).map((d) => {
            const [what, detail] = deliveryWords(d);
            const labels = d.members.map((m) => m.label);
            const about =
              labels.length > 1
                ? `${labels.length} events · ${[...new Set(labels)].join(", ")}`
                : (labels[0] ?? "");
            return (
              <li
                key={d.id}
                data-state={d.state}
                data-testid="connector-notify-delivery"
              >
                <b>{what}</b>
                <span className={styles.meta}>
                  {" · "}
                  {when(d.at)}
                  {about ? ` · ${about}` : ""}
                  {detail ? ` · ${detail}` : ""}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
