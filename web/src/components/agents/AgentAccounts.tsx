import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, type AgentAccount, type AgentAccountPool } from "../../lib/api";
import { SignInTerminal } from "../plugins/SignInTerminal";
import { accountSignIn } from "./accountSignIn";
import button from "../ui/actionButton.module.css";
import p from "../plugins/PluginSetup.module.css";
import s from "./AgentAccounts.module.css";

/** Settings → Agents → agent → Accounts (#1820 P1b).
 *
 *  Several logins of one agent. Each lives in a home only the vendor's CLI writes; BattleLab keeps
 *  a label and asks the CLI whether it is signed in. A NEW session starts on the account picked
 *  here and keeps it; a running or older session never changes account from this page.
 *
 *  Renders nothing for an agent the server does not list (its manifest names no account kind), so
 *  the section appears only once an agent is proven to hold several logins. */
const DEFAULT = "default";

type Signed = boolean | null | "checking";

function message(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

export function AgentAccounts({ engine, label }: { engine: string; label: string }) {
  const [pool, setPool] = useState<AgentAccountPool | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [signed, setSigned] = useState<Record<string, Signed>>({});
  const [signingIn, setSigningIn] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState<string | null>(null);

  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let alive = true;
    api
      .agentAccounts()
      .then((r) => alive && setPool(r.agents?.find((a) => a.engine === engine) ?? null))
      .catch(() => alive && setPool(null));
    return () => {
      alive = false;
    };
  }, [engine, nonce]);

  const check = useCallback(
    async (id: string) => {
      setSigned((m) => ({ ...m, [id]: "checking" }));
      try {
        const r = await api.agentAccountStatus(engine, id);
        setSigned((m) => ({ ...m, [id]: r.signed_in }));
      } catch {
        setSigned((m) => ({ ...m, [id]: null }));
      }
    },
    [engine],
  );

  const choose = async (account: string) => {
    setPicking(account);
    setBusy(true);
    setError(null);
    try {
      setPool(await api.setAgentAccountForNew(engine, account)); // the saved pool, as stored
    } catch (e) {
      setError(message(e)); // the stored choice shows again below
    } finally {
      setPicking(null);
      setBusy(false);
    }
  };

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setNonce((n) => n + 1);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  if (!pool) return null;
  // The click shows at once; a refused save puts the stored choice back (`choose`).
  const pick = picking ?? pool.new_sessions ?? DEFAULT;
  const max = pool.max_accounts ?? 5;
  const full = pool.accounts.length >= max;

  return (
    <div className={p.stack} data-testid="agent-accounts">
      <p className={s.lede}>
        Sign in to more than one {label} account. New sessions start on the account you choose
        below and keep it; a session that is already running stays on its own account.
      </p>
      {pool.error && (
        <p className={`${p.note} ${p.error}`} role="alert">
          {pool.error}
        </p>
      )}
      {error && (
        <p className={`${p.note} ${p.error}`} role="alert">
          {error}
        </p>
      )}

      <fieldset className={s.pick} disabled={busy}>
        <legend>New sessions start on</legend>
        <label className={p.check}>
          <input
            type="radio"
            name={`new-${engine}`}
            checked={pick === DEFAULT}
            onChange={() => void choose(DEFAULT)}
          />
          <span>Default login</span>
        </label>
        {pool.accounts.map((a) => (
          <label key={a.id} className={p.check}>
            <input
              type="radio"
              name={`new-${engine}`}
              checked={pick === a.id}
              disabled={(a.problems?.length ?? 0) > 0}
              onChange={() => void choose(a.id)}
            />
            <span>{a.label}</span>
          </label>
        ))}
      </fieldset>

      <ul className={s.list} aria-label={`${label} accounts`}>
        <li className={s.row}>
          <div className={s.who}>
            <span className={s.name}>Default login</span>
            <span className={s.meta}>The login {label} already has. BattleLab never changes it.</span>
          </div>
        </li>
        {pool.accounts.map((a) => (
          <AccountRow
            key={a.id}
            engine={engine}
            agentLabel={label}
            account={a}
            signed={signed[a.id]}
            busy={busy}
            signingIn={signingIn === a.id}
            onCheck={() => void check(a.id)}
            onSignIn={() => setSigningIn(a.id)}
            onSignInEnded={() => {
              void check(a.id);
            }}
            onCloseSignIn={() => setSigningIn(null)}
            onRename={(next) => run(() => api.renameAgentAccount(engine, a.id, next))}
            onRemove={() => run(() => api.removeAgentAccount(engine, a.id))}
          />
        ))}
      </ul>

      <AddAccount
        disabled={busy || full}
        full={full}
        max={max}
        onAdd={(name) =>
          run(async () => {
            const added = await api.addAgentAccount(engine, name);
            setSigningIn(added.id); // the next step is always the vendor's sign-in
          })
        }
      />
    </div>
  );
}

function AccountRow({
  engine,
  agentLabel,
  account,
  signed,
  busy,
  signingIn,
  onCheck,
  onSignIn,
  onSignInEnded,
  onCloseSignIn,
  onRename,
  onRemove,
}: {
  engine: string;
  agentLabel: string;
  account: AgentAccount;
  signed: Signed | undefined;
  busy: boolean;
  signingIn: boolean;
  onCheck: () => void;
  onSignIn: () => void;
  onSignInEnded: () => void;
  onCloseSignIn: () => void;
  onRename: (label: string) => Promise<void>;
  onRemove: () => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(account.label);
  const [confirming, setConfirming] = useState(false);
  const problems = account.problems ?? [];
  const state =
    problems.length > 0
      ? { tone: s.down, text: "Cannot be used" }
      : signed === true
        ? { tone: s.up, text: "Signed in" }
        : signed === false
          ? { tone: s.down, text: "Needs sign-in" }
          : signed === "checking"
            ? { tone: s.unknown, text: "Checking…" }
            : { tone: s.unknown, text: signed === null ? "Could not check" : "Not checked" };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    await onRename(draft);
    setEditing(false);
  };

  return (
    <li className={s.row} data-testid={`account-${account.id}`}>
      <div className={s.who}>
        {editing ? (
          <form className={p.row} onSubmit={submit}>
            <label className={p.field}>
              <span>Name</span>
              <input
                className={p.input}
                value={draft}
                maxLength={40}
                onChange={(e) => setDraft(e.target.value)}
                autoFocus
              />
            </label>
            <button className={button.primary} type="submit" disabled={busy || !draft.trim()}>
              Save
            </button>
            <button
              className={button.ghost}
              type="button"
              onClick={() => {
                setDraft(account.label);
                setEditing(false);
              }}
            >
              Cancel
            </button>
          </form>
        ) : (
          <span className={s.name}>{account.label}</span>
        )}
        <span className={s.state} role="status">
          <i className={`${s.dot} ${state.tone}`} aria-hidden="true" />
          {state.text}
        </span>
        {problems.length > 0 && (
          <ul className={s.problems}>
            {problems.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        )}
      </div>
      {!editing && (
        <div className={s.actions}>
          <button className={button.primary} type="button" disabled={busy || signingIn || problems.length > 0} onClick={onSignIn}>
            Sign in
          </button>
          <button className={button.ghost} type="button" disabled={busy || signed === "checking" || problems.length > 0} onClick={onCheck}>
            Check
          </button>
          <button className={button.ghost} type="button" disabled={busy} onClick={() => setEditing(true)}>
            Rename
          </button>
          {confirming ? (
            <>
              <button className={`${button.ghost} ${s.danger}`} type="button" disabled={busy} onClick={() => void onRemove()}>
                Forget account
              </button>
              <button className={button.ghost} type="button" onClick={() => setConfirming(false)}>
                Keep
              </button>
            </>
          ) : (
            <button className={button.ghost} type="button" disabled={busy || signingIn} onClick={() => setConfirming(true)}>
              Remove
            </button>
          )}
        </div>
      )}
      {confirming && (
        <p className={p.note} role="status">
          Sessions started on this account will no longer open, and this cannot be undone here:
          adding an account with the same name later creates a new account, which does not bring
          them back. Its sign-in stays on this machine.
        </p>
      )}
      {signingIn && (
        <div className={s.signin}>
          <p className={s.meta}>
            The vendor&apos;s own sign-in, for this account only. Choose the account you want here, not
            the one {agentLabel} already uses. Nothing in this terminal is kept or shown to HQ.
          </p>
          <SignInTerminal target={accountSignIn(engine, account.id)} onEnded={onSignInEnded} />
          <div className={p.row}>
            <button className={button.ghost} type="button" onClick={onCloseSignIn}>
              Close sign-in
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

function AddAccount({
  disabled,
  full,
  max,
  onAdd,
}: {
  disabled: boolean;
  full: boolean;
  max: number;
  onAdd: (label: string) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const label = name.trim();
    if (!label) return;
    await onAdd(label);
    setName("");
  };
  return (
    <form className={p.row} onSubmit={submit} aria-label="Add an account">
      <label className={p.field}>
        <span>New account name</span>
        <input
          className={p.input}
          value={name}
          maxLength={40}
          placeholder="Max 2"
          onChange={(e) => setName(e.target.value)}
          disabled={full}
        />
      </label>
      <button className={button.primary} type="submit" disabled={disabled || !name.trim()}>
        Add account
      </button>
      {full && <p className={s.meta}>An agent holds at most {max} accounts.</p>}
    </form>
  );
}
