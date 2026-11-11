/** A messenger connector's recipients and its send switch (#1832, #1746 P2a).
 *
 *  **The allowlist is built only from what the server lists.** The candidates come from the
 *  adapter (the chats or rooms the bot can see now) and the server keeps that list as a snapshot;
 *  the save sends ids only, and the server takes every label from its own reading. There is no
 *  field to type an id into, and nothing the client says about a chat is believed.
 *
 *  **Consent is bound to the bot.** Recipients chosen for one bot (or homeserver) are no consent
 *  for another: after such a change the server switches sending off and keeps the reason, the saved
 *  entries are shown as chosen for the old bot, and only a fresh save re-binds them.
 *
 *  **Sending is a separate switch**, enabled only once the connector is tested and has bound
 *  recipients. It lets HQ PROPOSE messages; every one still waits for a tap (#1833). */
import { useCallback, useState } from "react";

import { ApiError, api } from "../lib/api";
import {
  sendOffLine,
  type ConnectorInstance,
  type RecipientCandidate,
} from "../lib/connectors";

import { ConnectorAddresses } from "./ConnectorAddresses";

import styles from "./ConnectorsSettings.module.css";
import settings from "./Settings.module.css";

function message(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}


export function ConnectorRecipients(props: {
  inst: ConnectorInstance;
  /** Rooms (Matrix) or chats (Telegram): the words and the hint differ. */
  matrix: boolean;
  /** Mail (#1836): nothing to discover, so the Commander types each address. */
  email?: boolean;
  busy: boolean;
  onChanged: () => Promise<void>;
}) {
  return props.email ? (
    <ConnectorAddresses {...props} />
  ) : (
    <ListedRecipients {...props} />
  );
}

function ListedRecipients({
  inst,
  matrix,
  busy,
  onChanged,
}: {
  inst: ConnectorInstance;
  matrix: boolean;
  busy: boolean;
  onChanged: () => Promise<void>;
}) {
  const saved = inst.recipients ?? [];
  const bound = inst.recipients_bound ?? false;
  const [candidates, setCandidates] = useState<RecipientCandidate[] | null>(
    null,
  );
  const [chosen, setChosen] = useState<Set<string>>(
    () => new Set(saved.map((r) => r.id)),
  );
  const [working, setWorking] = useState<null | "refresh" | "save" | "send">(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [pendingSend, setPendingSend] = useState<boolean | null>(null);
  const ready = inst.status === "ready";
  const things = matrix ? "rooms" : "chats";

  const refresh = useCallback(async () => {
    setWorking("refresh");
    setError(null);
    try {
      const r = await api.connectorCandidates(inst.id);
      setCandidates(r.candidates);
      // A saved recipient the server now lists as unusable can't stay chosen: it is shown,
      // greyed with its reason, and the next save leaves it out. After a change of bot or
      // origin the same goes for one the new bot doesn't list at all: the server re-binds only
      // what this bot can see (#1832, Hermes 7603), so it is never kept out of sight.
      const listed = new Set(r.candidates.map((c) => c.id));
      setChosen((prev) => {
        const next = new Set(prev);
        for (const c of r.candidates) if (c.unavailable) next.delete(c.id);
        if (!bound) for (const id of prev) if (!listed.has(id)) next.delete(id);
        return next;
      });
    } catch (e) {
      setError(message(e, `Couldn’t list the ${things} this bot can see.`));
    } finally {
      setWorking(null);
    }
  }, [inst.id, things, bound]);

  const save = async () => {
    setWorking("save");
    setError(null);
    try {
      await api.setConnectorRecipients(inst.id, [...chosen]);
      await onChanged();
    } catch (e) {
      setError(message(e, "Couldn’t save the recipients."));
    } finally {
      setWorking(null);
    }
  };

  const toggleSend = (on: boolean) => {
    setPendingSend(on);
    setWorking("send");
    setError(null);
    void api
      .setConnectorSend(inst.id, on)
      .catch((err: unknown) => setError(message(err, "That didn’t work.")))
      .then(onChanged)
      .finally(() => {
        setWorking(null);
        setPendingSend(null);
      });
  };

  // What the list shows: the server's candidates once listed, else the saved recipients. A saved
  // recipient missing from a fresh list stays visible: the same bot may keep it (a quiet chat);
  // another bot can't, so it is greyed with the reason and the next save leaves it out.
  const unlisted: RecipientCandidate[] = candidates
    ? saved
        .filter((r) => !candidates.some((c) => c.id === r.id))
        .map((r) => ({
          ...r,
          allowed: true,
          unavailable: bound
            ? undefined
            : `this bot can’t see it, so saving removes it`,
        }))
    : [];
  const shown: RecipientCandidate[] = candidates
    ? [...candidates, ...unlisted]
    : saved.map((r) => ({ ...r, allowed: true }));
  const savedIds = saved
    .map((r) => r.id)
    .sort()
    .join("\n");
  const dirty = [...chosen].sort().join("\n") !== savedIds;
  // After a change of bot or origin, saving the same list again is what re-binds it.
  const canSave =
    working === null &&
    !busy &&
    ready &&
    candidates !== null &&
    (dirty || !bound);
  const canSend = ready && saved.length > 0 && bound;
  const offLine = sendOffLine(inst);

  return (
    <div className={styles.read} data-testid="connector-recipients">
      <h4 className={settings.subhead}>Recipients HQ may message</h4>
      <p className={settings.hint}>
        {matrix
          ? "Invite the bot to a room, then Refresh. Encrypted rooms can’t be used yet."
          : "Send any message to your bot from the chat you want, then Refresh."}
      </p>
      {offLine && (
        <p
          className={`${settings.callout} ${settings.calloutWarn}`}
          role="note"
          data-testid="connector-send-off"
        >
          {offLine}
        </p>
      )}
      {error && (
        <p
          className={settings.err}
          role="alert"
          data-testid="connector-recipients-error"
        >
          {error}
        </p>
      )}
      {shown.length === 0 ? (
        <p className={settings.hint} data-testid="connector-recipients-empty">
          {candidates
            ? matrix
              ? "No rooms yet. Invite the bot to a room, then Refresh."
              : "No chats yet. Send any message to your bot from the chat you want, then Refresh."
            : `No recipients yet. Refresh to see the ${things} this bot can reach.`}
        </p>
      ) : (
        <ul className={styles.recipients}>
          {shown.map((c) => (
            <li
              key={c.id}
              className={styles.recipient}
              data-testid="connector-recipient"
              data-unavailable={c.unavailable ? "true" : undefined}
            >
              <label className={settings.aiToggle}>
                <input
                  type="checkbox"
                  checked={chosen.has(c.id)}
                  disabled={
                    !!c.unavailable || working !== null || busy || !candidates
                  }
                  onChange={(e) => {
                    const next = new Set(chosen);
                    if (e.target.checked) next.add(c.id);
                    else next.delete(c.id);
                    setChosen(next);
                  }}
                />
                <span>
                  <b>{c.label}</b>
                  <span className={styles.meta}>
                    {" "}
                    · {c.kind}
                    {c.members ? ` · ${c.members} people` : ""}
                    {c.unavailable ? ` · can’t be used: ${c.unavailable}` : ""}
                    {!bound && c.allowed && inst.recipients_chosen_for
                      ? ` · chosen for ${inst.recipients_chosen_for}`
                      : ""}
                  </span>
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}
      <div className={styles.actions}>
        <button
          type="button"
          className={settings.secBtnGhost}
          disabled={!ready || working !== null || busy}
          onClick={() => void refresh()}
        >
          {working === "refresh" ? "Looking…" : "Refresh"}
        </button>
        <button
          type="button"
          className={settings.secBtn}
          disabled={!canSave}
          onClick={() => void save()}
        >
          {working === "save" ? "Saving…" : "Save recipients"}
        </button>
      </div>
      <label className={settings.aiToggle}>
        <input
          type="checkbox"
          checked={pendingSend ?? !!inst.send}
          disabled={working !== null || busy || (!canSend && !inst.send)}
          onChange={(e) => toggleSend(e.target.checked)}
        />
        <span>Let HQ propose messages</span>
      </label>
      <p className={canSend ? settings.callout : settings.hint}>
        {canSend
          ? "HQ never sends on its own: every message waits for your tap on its card in HQ, and goes only to the recipients above."
          : "Save at least one recipient after a passing test to switch this on."}
      </p>
    </div>
  );
}
