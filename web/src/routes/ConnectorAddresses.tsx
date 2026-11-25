/** An email connector's From address, its addresses and its send switch (#1836, #1746 P2b).
 *
 *  **Mail has nothing to discover**, so the Commander types each address. The page checks an
 *  address in words as it is added, but the SERVER decides: the save sends the whole list with the
 *  generation the page showed, and the server refuses anything that is not one plain mailbox (no
 *  display name, no list, no spaces), lower-cases the domain, and refuses a duplicate.
 *
 *  **The From address is part of what a card was approved for.** Saving a new one puts the
 *  connector out of test (a waiting card turns stale) and is followed by a test at once.
 *
 *  **Consent is bound to the account**, as #1832's is to the bot: after the connector signs in as
 *  another account the server switches sending off and keeps why, and the addresses are flagged
 *  until they are saved again. Every message still waits for a tap (#1833). */
import { useState } from "react";

import { ApiError, api } from "../lib/api";
import { sendOffLine, type ConnectorInstance } from "../lib/connectors";

import styles from "./ConnectorsSettings.module.css";
import settings from "./Settings.module.css";

type Row = { address: string; label: string; saved: boolean };

const ATOM = "[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+";
const LABEL = "[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?";
/** The server's rule (`store.ADDRESS_RE`), for words before a round trip; the server decides. */
const ADDRESS = new RegExp(`^${ATOM}(?:\\.${ATOM})*@${LABEL}(?:\\.${LABEL})+$`);

function message(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

/** The address as the server stores it (the domain lower-cased), or why it is not one. */
function checkAddress(raw: string): { address: string } | { why: string } {
  if (!raw) return { why: "Enter an address, like name@example.com." };
  if (/\s/.test(raw))
    return {
      why: `“${raw}” has a space or a line break in it: write it as name@example.com.`,
    };
  if (/[<>"(),;]/.test(raw))
    return {
      why: `“${raw}” isn’t one plain address: write it as name@example.com, without a name, quotes or a list.`,
    };
  const at = raw.lastIndexOf("@");
  const address =
    at < 0 ? raw : raw.slice(0, at + 1) + raw.slice(at + 1).toLowerCase();
  if (
    at < 0 ||
    at > 64 ||
    address.length > 254 ||
    !ADDRESS.test(address)
  )
    return { why: `“${raw}” isn’t an address, like name@example.com.` };
  return { address };
}

function fromSaved(inst: ConnectorInstance): Row[] {
  return (inst.recipients ?? []).map((r) => ({
    address: r.id,
    label: r.label === r.id ? "" : r.label,
    saved: true,
  }));
}

export function ConnectorAddresses({
  inst,
  busy,
  onChanged,
}: {
  inst: ConnectorInstance;
  busy: boolean;
  onChanged: () => Promise<void>;
}) {
  const ready = inst.status === "ready";
  const bound = inst.recipients_bound ?? false;
  const savedKey = JSON.stringify(
    (inst.recipients ?? []).map((r) => [r.id, r.label]),
  );
  /** The list as the server last answered it. A save (or a reload) that changes it replaces the
   *  rows; edits stay put while the server's list is the one they started from. */
  const [base, setBase] = useState(savedKey);
  const [rows, setRows] = useState<Row[]>(() => fromSaved(inst));
  if (savedKey !== base) {
    setBase(savedKey);
    setRows(fromSaved(inst));
  }
  const [sender, setSender] = useState(inst.sender ?? "");
  const [baseSender, setBaseSender] = useState(inst.sender ?? "");
  if ((inst.sender ?? "") !== baseSender) {
    setBaseSender(inst.sender ?? "");
    if (sender === baseSender) setSender(inst.sender ?? "");
  }
  const [draft, setDraft] = useState("");
  const [draftLabel, setDraftLabel] = useState("");
  const [why, setWhy] = useState<string | null>(null);
  const [working, setWorking] = useState<null | "from" | "save" | "send">(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [pendingSend, setPendingSend] = useState<boolean | null>(null);
  const ids = {
    from: `${inst.id}-from`,
    add: `${inst.id}-addr`,
    label: `${inst.id}-addr-label`,
  };

  const shownRows = (list: Row[]) =>
    JSON.stringify(list.map((r) => [r.address, r.label || r.address]));
  const dirty = shownRows(rows) !== savedKey;
  const draftSender = sender.trim();
  const senderDirty = draftSender !== (inst.sender ?? "");
  const senderCheck = draftSender ? checkAddress(draftSender) : null;
  const idle = working === null && !busy;

  const add = () => {
    const got = checkAddress(draft.trim());
    if ("why" in got) {
      setWhy(got.why);
      return;
    }
    if (rows.some((r) => r.address === got.address)) {
      setWhy(`${got.address} is already in the list.`);
      return;
    }
    if (rows.length >= 20) {
      setWhy("HQ may write to at most 20 addresses.");
      return;
    }
    setRows([
      ...rows,
      { address: got.address, label: draftLabel.trim(), saved: false },
    ]);
    setDraft("");
    setDraftLabel("");
    setWhy(null);
  };

  const act = async (what: "from" | "save", fn: () => Promise<void>) => {
    setWorking(what);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(message(e, "That didn’t work."));
    } finally {
      await onChanged();
      setWorking(null);
    }
  };

  const saveSender = () =>
    act("from", async () => {
      await api.updateConnector(inst.id, { sender: draftSender });
      // A new From address puts the connector out of test: test it again at once.
      const r = await api.testConnector(inst.id);
      if (r.status === "failed") setError(r.detail || "The test didn’t pass.");
    });

  const saveRows = () =>
    act("save", async () => {
      await api.setConnectorAddresses(
        inst.id,
        rows.map((r) => ({ address: r.address, label: r.label })),
        inst.generation,
      );
    });

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

  const savedCount = inst.recipients?.length ?? 0;
  const canSave = idle && ready && (dirty || (!bound && savedCount > 0));
  const canSend = ready && savedCount > 0 && bound && !!inst.sender;
  const offLine = sendOffLine(inst);

  return (
    <div className={styles.read} data-testid="connector-recipients">
      <div className={settings.aiField}>
        <label className={settings.aiFieldLabel} htmlFor={ids.from}>
          From address
        </label>
        <div className={settings.aiKeyRow}>
          <input
            id={ids.from}
            className={settings.aiInput}
            type="email"
            spellCheck={false}
            autoComplete="off"
            autoCapitalize="off"
            placeholder="hq@example.com"
            value={sender}
            onChange={(e) => setSender(e.target.value)}
          />
          <button
            type="button"
            className={settings.secBtn}
            disabled={
              !idle ||
              !senderDirty ||
              !draftSender ||
              (senderCheck !== null && "why" in senderCheck)
            }
            onClick={() => void saveSender()}
            data-testid="connector-sender-save"
          >
            {working === "from" ? "Saving…" : "Save"}
          </button>
        </div>
        {senderDirty && senderCheck && "why" in senderCheck ? (
          <p className={settings.err} role="alert">
            {senderCheck.why}
          </p>
        ) : (
          <p className={settings.hint}>
            The sender every message shows. Changing it puts any message
            waiting for your tap out of date.
          </p>
        )}
      </div>

      <h4 className={settings.subhead}>Addresses HQ may write to</h4>
      <p className={settings.hint}>
        Mail has nothing to look up, so you type each address. HQ can only
        propose mail to these, and every message waits for your tap.
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
      {rows.length === 0 ? (
        <p className={settings.hint} data-testid="connector-recipients-empty">
          No addresses yet. Add one below.
        </p>
      ) : (
        <ul className={styles.recipients}>
          {rows.map((r) => (
            <li
              key={r.address}
              className={`${styles.recipient} ${styles.address}`}
              data-testid="connector-recipient"
              data-unsaved={r.saved ? undefined : "true"}
            >
              <span className={styles.addressText}>
                <b>{r.label || r.address}</b>
                <span className={styles.meta}>
                  {r.label ? ` · ${r.address}` : ""}
                  {!r.saved ? " · not saved yet" : ""}
                  {r.saved && !bound && inst.recipients_chosen_for
                    ? ` · chosen for ${inst.recipients_chosen_for}`
                    : ""}
                </span>
              </span>
              <button
                type="button"
                className={settings.secBtnGhost}
                disabled={!idle}
                aria-label={`Remove ${r.address}`}
                onClick={() =>
                  setRows(rows.filter((x) => x.address !== r.address))
                }
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className={settings.aiField}>
        <label className={settings.aiFieldLabel} htmlFor={ids.add}>
          Add an address
        </label>
        <div className={settings.aiKeyRow}>
          <input
            id={ids.add}
            className={settings.aiInput}
            type="email"
            spellCheck={false}
            autoComplete="off"
            autoCapitalize="off"
            placeholder="name@example.com"
            value={draft}
            aria-invalid={why ? true : undefined}
            onChange={(e) => {
              setDraft(e.target.value);
              setWhy(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add();
              }
            }}
          />
          <input
            id={ids.label}
            className={settings.aiInput}
            aria-label="Name for this address (optional)"
            autoComplete="off"
            placeholder="Name (optional)"
            maxLength={100}
            value={draftLabel}
            onChange={(e) => setDraftLabel(e.target.value)}
          />
          <button
            type="button"
            className={settings.secBtn}
            disabled={!idle || !draft.trim()}
            onClick={add}
          >
            Add
          </button>
        </div>
        {why && (
          <p
            className={settings.err}
            role="alert"
            data-testid="connector-address-invalid"
          >
            {why}
          </p>
        )}
      </div>
      <div className={styles.actions}>
        <button
          type="button"
          className={settings.secBtn}
          disabled={!canSave}
          onClick={() => void saveRows()}
        >
          {working === "save" ? "Saving…" : "Save addresses"}
        </button>
      </div>

      <label className={settings.aiToggle}>
        <input
          type="checkbox"
          checked={pendingSend ?? !!inst.send}
          disabled={!idle || (!canSend && !inst.send)}
          onChange={(e) => toggleSend(e.target.checked)}
        />
        <span>Let HQ propose mail</span>
      </label>
      <p className={canSend ? settings.callout : settings.hint}>
        {canSend
          ? "HQ never sends a proposed message on its own: each one waits for your tap on its card in HQ, goes only to the addresses above, and mail can’t be taken back once it’s sent. Notifications (below) are the only thing sent without a tap, and only to the one address you switch on."
          : "Set a From address and save at least one address after a passing test to switch this on."}
      </p>
    </div>
  );
}
