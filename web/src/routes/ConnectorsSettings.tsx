/** Settings → AI → HQ connectors (#1746 P1b): add a connector from the bundled catalog, give it
 *  its address and token, test it, and switch reading on.
 *
 *  **The server owns every state.** Each card renders the instance exactly as `/api/connectors`
 *  last answered it and re-reads after every action, so the page never shows a switch the store
 *  would refuse: reading can only be turned on for a `ready` connector, and any edit to the
 *  address or token makes it untested again on the server, which this page then shows.
 *
 *  **The token is write-only**, like the AI endpoint's key and the forge connection's token
 *  (`ForgeSettings.tsx`): it is never prefilled, the instance carries `token_set` only, and the
 *  field is cleared after a save only if it still holds what was sent.
 *
 *  **A new address needs the token again.** A stored token is only ever sent to the address it was
 *  saved for (`connectors/store.py`, enforced inside the store's lock). The page asks for it up
 *  front instead of letting the save be refused. */
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "../lib/api";
import {
  failedNow,
  readsLabel,
  statusLine,
  testFailure,
  type ConnectorCatalog,
  type ConnectorInstance,
  type ConnectorManifest,
} from "../lib/connectors";

import styles from "./ConnectorsSettings.module.css";
import settings from "./Settings.module.css";

/** The connection test reads the first page of the forge's repository search, which holds at most
 *  this many (`connectors/adapters/forgejo.py` `SEARCH_LIMIT`). A full page means "at least". */
const REPO_PAGE = 20;

function message(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

export function ConnectorsSettings() {
  const [data, setData] = useState<ConnectorCatalog | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [addError, setAddError] = useState<string | null>(null);
  /** The card just added, whose address field takes focus once it renders. */
  const [fresh, setFresh] = useState<string | null>(null);

  /** Which read is the latest. Reads overlap (one card's refresh while another card acts), and
   *  replies can arrive out of order: only the newest read may install its answer, so an older
   *  snapshot can never put back a state the store has since left — say, "reading is off" while
   *  HQ is reading (Hermes on #1828). */
  const latestRead = useRef(0);
  const reload = useCallback(() => {
    const seq = ++latestRead.current;
    return api
      .connectors()
      .then((r) => {
        if (seq !== latestRead.current) return;
        setData(r);
        setLoadError(null);
      })
      .catch((e: unknown) => {
        if (seq === latestRead.current)
          setLoadError(message(e, "Couldn’t load your connectors."));
      });
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);

  const add = async (m: ConnectorManifest) => {
    setAdding(m.id);
    setAddError(null);
    try {
      const inst = await api.addConnector(m.id);
      setFresh(inst.id);
    } catch (e) {
      setAddError(message(e, `Couldn’t add ${m.label}.`));
    } finally {
      setAdding(null);
      await reload();
    }
  };

  const manifests = new Map((data?.catalog ?? []).map((m) => [m.id, m]));
  const instances = data?.instances ?? [];
  return (
    <section
      className={settings.section}
      aria-labelledby="connectors-h"
      data-testid="connectors-settings"
    >
      <h2 id="connectors-h">HQ connectors</h2>
      <p className={settings.hint}>
        Connectors let HQ read from your other services: ask it about an issue,
        a pull request, a commit’s checks or a file in your cloud, and it looks
        them up. They <b>only read</b>. Nothing is sent, commented, merged or
        changed through a connector. You sign in with your own account, so HQ
        sees what that account can see.
      </p>
      {loadError && (
        <p className={settings.err} role="alert">
          {loadError}
        </p>
      )}

      <h3 className={settings.subhead}>Your connectors</h3>
      {data && instances.length === 0 && (
        <p className={settings.hint} data-testid="connectors-empty">
          No connectors yet, so HQ can’t read from any service. Add one below.
        </p>
      )}
      {instances.length > 0 && (
        <ul className={styles.list}>
          {instances.map((inst) => (
            <ConnectorCard
              key={inst.id}
              inst={inst}
              manifest={manifests.get(inst.manifest_id)}
              disclosure={data?.disclosure ?? ""}
              focusOnMount={fresh === inst.id}
              onChanged={reload}
            />
          ))}
        </ul>
      )}

      <h3 className={settings.subhead}>Add a connector</h3>
      {addError && (
        <p className={settings.err} role="alert">
          {addError}
        </p>
      )}
      <ul className={styles.catalog} data-testid="connectors-catalog">
        {(data?.catalog ?? []).map((m) => (
          <li key={m.id} className={styles.entry}>
            <div className={styles.entryText}>
              <b className={styles.name}>{m.label}</b>
              <span className={styles.meta}>
                by {m.publisher === "battlelab" ? "BattleLab" : m.publisher} ·
                reads {readsLabel(m.capabilities.read)}
              </span>
            </div>
            <button
              type="button"
              className={settings.secBtnGhost}
              disabled={adding !== null}
              onClick={() => void add(m)}
            >
              {adding === m.id ? "Adding…" : `Add ${m.label}`}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

type Busy = null | "save" | "test" | "read" | "remove";

function ConnectorCard({
  inst,
  manifest,
  disclosure,
  focusOnMount,
  onChanged,
}: {
  inst: ConnectorInstance;
  manifest: ConnectorManifest | undefined;
  disclosure: string;
  focusOnMount: boolean;
  onChanged: () => Promise<void>;
}) {
  const label = manifest?.label ?? inst.manifest_id;
  const fixed = !!manifest?.fixed_origin;
  /** A `basic` connector (#1829) signs in with an account name and an app password. */
  const basic = manifest?.auth.kind === "basic";
  const secretName = basic ? "app password" : "token";
  const [account, setAccount] = useState(inst.account ?? "");
  const [baseAccount, setBaseAccount] = useState(inst.account ?? "");
  if ((inst.account ?? "") !== baseAccount) {
    setBaseAccount(inst.account ?? "");
    if (account === baseAccount) setAccount(inst.account ?? "");
  }
  const [origin, setOrigin] = useState(inst.origin);
  /** The server's address the draft was last taken from. A reload that moves it adopts the new
   *  value only while the draft is untouched: a stored change never overwrites typing. */
  const [base, setBase] = useState(inst.origin);
  if (inst.origin !== base) {
    setBase(inst.origin);
    if (origin === base) setOrigin(inst.origin);
  }
  const [token, setToken] = useState("");
  /** The live field, for a save that resolves after more typing (`ForgeSettings.tsx`, #897). */
  const tokenRef = useRef("");
  const [editingToken, setEditingToken] = useState(!inst.token_set);
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  /** A failed test's own words, for the generation it tested. The stored `last_test` keeps only a
   *  code; the reply's `detail` names the host and status, so it is shown while still current. */
  const [failure, setFailure] = useState<{
    generation: number;
    text: string;
  } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  /** The last passing test's warning (a token wider than reading needs), for its generation. */
  const [warning, setWarning] = useState<{
    generation: number;
    text: string;
  } | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  /** The switch's requested position while the server is asked, so it moves when tapped. It falls
   *  back to the server's answer once the re-read lands, whichever way that went. */
  const [pendingRead, setPendingRead] = useState<boolean | null>(null);
  const originRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (focusOnMount) originRef.current?.focus();
  }, [focusOnMount]);

  const ids = {
    origin: `${inst.id}-origin`,
    account: `${inst.id}-account`,
    token: `${inst.id}-token`,
    read: `${inst.id}-read`,
    head: `${inst.id}-h`,
  };
  const draftOrigin = origin.trim();
  const typedToken = editingToken && token.trim() !== "" ? token : null;
  const originDirty = !fixed && draftOrigin !== inst.origin;
  const draftAccount = account.trim();
  const accountDirty = basic && draftAccount !== (inst.account ?? "");
  const dirty = originDirty || accountDirty || typedToken !== null;
  /** The stored secret stays bound to the stored address (and account): a move brings a new one. */
  const needsTokenAgain =
    (originDirty || accountDirty) && inst.token_set && typedToken === null;
  const missing = !draftOrigin
    ? "address"
    : basic && !draftAccount
      ? "account"
      : !inst.token_set && typedToken === null
        ? "token"
        : null;
  const canSave = busy === null && dirty && !needsTokenAgain && !missing;
  const canTest =
    busy === null && !dirty && inst.origin !== "" && inst.token_set;

  const run = async (what: Busy, fn: () => Promise<void>) => {
    setBusy(what);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      setError(message(e, "That didn’t work."));
    } finally {
      // Busy until the refresh has landed: the controls stay locked while the page still shows
      // the state from before this action.
      await onChanged();
      setBusy(null);
    }
  };

  const test = async () => {
    const r = await api.testConnector(inst.id);
    if (r.status === "failed")
      setFailure({
        generation: r.generation,
        text: testFailure(r.code, r.detail),
      });
    else setFailure(null);
    setWarning(
      r.status === "ok" && r.warning
        ? { generation: r.generation, text: r.warning }
        : null,
    );
    if (r.status === "stale")
      setNotice(
        "The connector changed while it was being tested. Test it again.",
      );
  };

  const saveAndTest = () =>
    run("save", async () => {
      const body: { origin?: string; token?: string; account?: string } = {};
      if (!fixed) body.origin = draftOrigin;
      if (basic) body.account = draftAccount;
      if (typedToken !== null) body.token = typedToken;
      await api.updateConnector(inst.id, body);
      if (typedToken !== null && tokenRef.current === typedToken) {
        tokenRef.current = "";
        setToken("");
        setEditingToken(false);
      }
      setBusy("test");
      await test();
    });

  const shown = statusLine(inst);
  const failedText =
    failure && failure.generation === inst.generation
      ? failure.text
      : failedNow(inst)
        ? testFailure(inst.last_test?.code)
        : null;
  const ready = inst.status === "ready";
  const repos = inst.tested
    ? `${inst.tested.repos}${inst.tested.repos >= REPO_PAGE ? "+" : ""}`
    : "";

  return (
    <li
      className={styles.card}
      aria-labelledby={ids.head}
      data-testid="connector-card"
      data-status={inst.status}
    >
      <div className={styles.head}>
        <span className={`hud-led ${shown.tone}`} aria-hidden="true" />
        <div className={styles.headText}>
          <b id={ids.head} className={styles.name}>
            {label}
            {inst.origin && (
              <span className={styles.origin}>
                {" "}
                · {inst.origin.replace(/^https?:\/\//, "")}
              </span>
            )}
          </b>
          <span className={styles.status} data-testid="connector-status">
            {shown.text}
          </span>
        </div>
      </div>

      {error && (
        <p className={settings.err} role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className={settings.hint} role="status">
          {notice}
        </p>
      )}

      {inst.status === "unavailable" ? (
        <p className={settings.hint}>
          This BattleLab no longer ships the {label} connector, so HQ can’t read
          through it. Remove it; its token is deleted with it.
        </p>
      ) : (
        <>
          <div className={settings.aiField}>
            <label className={settings.aiFieldLabel} htmlFor={ids.origin}>
              Address
            </label>
            <input
              id={ids.origin}
              ref={originRef}
              className={settings.aiInput}
              type="url"
              inputMode="url"
              spellCheck={false}
              autoComplete="off"
              placeholder="https://git.example.com"
              value={fixed ? (manifest?.fixed_origin ?? "") : origin}
              disabled={fixed}
              onChange={(e) => setOrigin(e.target.value)}
            />
          </div>

          {basic && (
            <div className={settings.aiField}>
              <label className={settings.aiFieldLabel} htmlFor={ids.account}>
                Account
              </label>
              <input
                id={ids.account}
                className={settings.aiInput}
                spellCheck={false}
                autoComplete="off"
                autoCapitalize="off"
                placeholder="the name you sign in with"
                value={account}
                onChange={(e) => setAccount(e.target.value)}
              />
            </div>
          )}

          <div className={settings.aiField}>
            <label
              className={settings.aiFieldLabel}
              {...(editingToken ? { htmlFor: ids.token } : {})}
            >
              {basic ? "App password" : "Access token"}
              {inst.token_set && (
                <span className={settings.aiKeyBadge}>saved</span>
              )}
            </label>
            <div className={settings.aiKeyRow}>
              {editingToken ? (
                <input
                  id={ids.token}
                  className={settings.aiInput}
                  type="password"
                  autoComplete="new-password"
                  data-1p-ignore="true"
                  data-lpignore="true"
                  data-bwignore="true"
                  value={token}
                  onChange={(e) => {
                    tokenRef.current = e.target.value;
                    setToken(e.target.value);
                  }}
                />
              ) : (
                <button
                  type="button"
                  className={settings.secBtnGhost}
                  onClick={() => setEditingToken(true)}
                >
                  Replace {secretName}
                </button>
              )}
            </div>
            {editingToken && (
              <p className={settings.hint}>
                {manifest?.setup ??
                  "Create a read-only token for your account."}
                {manifest && manifest.auth.scopes.length > 0 && (
                  <>
                    {" "}
                    Scopes: <code>{manifest.auth.scopes.join(", ")}</code>.
                  </>
                )}
              </p>
            )}
            {needsTokenAgain && (
              <p
                className={`${settings.callout} ${settings.calloutWarn}`}
                role="note"
              >
                {accountDirty && !originDirty
                  ? `A new account needs the app password again. The saved one is only ever sent for ${inst.account}.`
                  : `A new address needs the ${secretName} again. The saved one is only ever sent to ${inst.origin}.`}
              </p>
            )}
          </div>

          <div className={styles.actions}>
            {dirty || !inst.token_set || !inst.origin ? (
              <button
                type="button"
                className={settings.secBtn}
                disabled={!canSave}
                onClick={() => void saveAndTest()}
              >
                {busy === "save"
                  ? "Saving…"
                  : busy === "test"
                    ? "Testing…"
                    : "Save and test"}
              </button>
            ) : (
              <button
                type="button"
                className={ready ? settings.secBtnGhost : settings.secBtn}
                disabled={!canTest}
                onClick={() => void run("test", test)}
              >
                {busy === "test" ? "Testing…" : ready ? "Test again" : "Test"}
              </button>
            )}
          </div>

          {failedText && (
            <p
              className={settings.err}
              role="alert"
              data-testid="connector-test-failed"
            >
              {failedText}
            </p>
          )}
          {ready && warning && warning.generation === inst.generation && (
            <p
              className={`${settings.callout} ${settings.calloutWarn}`}
              role="note"
              data-testid="connector-test-warning"
            >
              {warning.text}
            </p>
          )}
          {ready && inst.tested && (
            <p className={settings.ok} data-testid="connector-tested">
              {inst.tested.login ? (
                <>
                  Signed in as <b>{inst.tested.login}</b>
                </>
              ) : (
                "Token accepted"
              )}{" "}
              · {repos}{" "}
              {manifest?.category === "storage"
                ? `${inst.tested.repos === 1 ? "item" : "items"} in your files`
                : `${inst.tested.repos === 1 ? "repository" : "repositories"} visible`}
            </p>
          )}

          <div className={styles.read}>
            <label className={settings.aiToggle} htmlFor={ids.read}>
              <input
                id={ids.read}
                type="checkbox"
                checked={pendingRead ?? inst.enabled.read}
                disabled={busy !== null || (!ready && !inst.enabled.read)}
                onChange={(e) => {
                  const on = e.target.checked;
                  setPendingRead(on);
                  void run("read", async () => {
                    await api.setConnectorRead(inst.id, on);
                  }).finally(() => setPendingRead(null));
                }}
              />
              <span>Let HQ read through this connector</span>
            </label>
            {!ready && (
              <p className={settings.hint}>
                HQ can’t read through it until a test passes.
              </p>
            )}
            {ready && disclosure && (
              <p
                className={settings.aiEgress}
                data-testid="connector-disclosure"
              >
                {disclosure}
              </p>
            )}
          </div>
        </>
      )}

      <div className={styles.foot}>
        {confirmRemove ? (
          <div
            className={settings.confirmRow}
            role="group"
            aria-label={`Confirm removing ${label}`}
          >
            <span className={settings.confirmText}>
              Remove this connector? Its token is deleted.
            </span>
            <button
              type="button"
              className={settings.danger}
              disabled={busy !== null}
              onClick={() =>
                void run("remove", async () => {
                  await api.removeConnector(inst.id);
                })
              }
            >
              {busy === "remove" ? "Removing…" : "Remove connector"}
            </button>
            <button
              type="button"
              className={settings.secBtnGhost}
              onClick={() => setConfirmRemove(false)}
            >
              Keep it
            </button>
          </div>
        ) : (
          <button
            type="button"
            className={settings.secBtnGhost}
            disabled={busy !== null}
            onClick={() => setConfirmRemove(true)}
          >
            Remove
          </button>
        )}
      </div>
    </li>
  );
}
