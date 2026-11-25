/** Where MISSION CONTROL looks for the facts its objectives are about (#891).
 *
 * A mission's checklist says "a PR is open", "checks are green", "it is merged". Those are facts a
 * server can check — but only against a forge it has been pointed at. Without this panel the probe
 * runner would exist and answer `unknown` for ever, which is the shape of half-shipped work #889
 * was about: a reviewed server capability with nothing able to reach it.
 *
 * **The token is write-only.** It is stored server-side and never comes back: the config carries
 * `token_set`, and the field is a reveal-to-edit control exactly like the AI endpoint's key, so a
 * form that round-trips a masked value cannot silently erase a working credential.
 *
 * **The primary control is "Save connection", not "Save".** Settings already has a `Save` button
 * (the prompt editor's), and a second one with the same accessible name makes every
 * `getByRole("button", {name: "Save", exact: true})` on the page ambiguous — which is exactly how
 * this broke an unrelated spec in CI. It is also the better label: a page with two identical Save
 * buttons is ambiguous to a person, not only to a locator.
 *
 * **Not configured is not "broken".** An unconfigured forge makes every forge probe answer
 * `unknown` — objectives stay visibly unmet and stale with a reason — rather than `failed`. The
 * copy says that, because "no forge configured" reading as "your checks are red" would be the same
 * class of lie the probes themselves are built to avoid.
 */
import { useCallback, useRef, useState } from "react";

import { useConfig, useConfigRefresh } from "../app/config";
import { ApiError, api } from "../lib/api";

import styles from "./Settings.module.css";

const KINDS: { id: string; label: string }[] = [
  { id: "forgejo", label: "Forgejo" },
  { id: "gitea", label: "Gitea" },
  { id: "github", label: "GitHub" },
];

export function ForgeSettings() {
  const block = useConfig()?.forge;
  const refreshConfig = useConfigRefresh();

  const [enabled, setEnabled] = useState(false);
  const [kind, setKind] = useState("forgejo");
  const [baseUrl, setBaseUrl] = useState("");
  const [owner, setOwner] = useState("");
  const [token, setToken] = useState("");
  /** The live value, for callbacks that resolve later. A captured `token` answers the question as
   *  it was when the request started — the moment that does not matter. */
  const tokenRef = useRef("");
  const [editingToken, setEditingToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // Adopt the server's values once the config lands — it can arrive after mount, and a form
  // seeded from `undefined` would quietly offer to save an empty base URL over a working one.
  //
  // Adjusted DURING RENDER rather than in an effect, which is the pattern `Pulse.tsx` already uses
  // for the same problem: React re-renders immediately with the new state and nothing in between
  // ever paints, where an effect renders the empty form first and then corrects it. The lint rule
  // (`react-hooks/set-state-in-effect`) names the same thing.
  const [synced, setSynced] = useState(false);
  if (block && !synced) {
    setSynced(true);
    setEnabled(!!block.enabled);
    setKind(block.kind || "forgejo");
    setBaseUrl(block.base_url || "");
    setOwner(block.owner || "");
  }

  const save = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      // `token` is sent ONLY when the operator actually typed one. Omitting the key preserves
      // what is stored; sending "" would too, but not sending it at all is the clearer contract
      // and keeps a stored credential out of a request that had no reason to carry one.
      const patch: Record<string, unknown> = {
        enabled,
        kind,
        base_url: baseUrl.trim(),
        owner: owner.trim(),
      };
      const submitted = editingToken && token ? token : null;
      if (submitted) patch.token = submitted;
      await api.setPrefs({ forge: patch });
      // CLEAR ONLY WHAT WAS SENT. The field stays editable while the request is in flight, so an
      // operator who submits token A and types B before A resolves would otherwise have B wiped
      // unsaved by the success path (#897 review). Compared against the SUBMITTED snapshot rather
      // than trusted to be unchanged.
      if (submitted !== null) {
        // Only if the field still holds what was actually sent. Typing continues during the
        // request, and closing the editor over a newer draft loses it just as surely as clearing
        // the value would.
        //
        // Read from the REF, not from `token`: the closure captured `token` when the save
        // started, which is the one moment that cannot answer "has it changed since?".
        if (tokenRef.current === submitted) {
          tokenRef.current = "";
          setToken("");
          setEditingToken(false);
        }
      }
      setSaved(true);
      // Without this the panel keeps rendering the config it mounted with, and `token_set` never
      // flips — the "my settings don't save" shape that has bitten three panels already.
      refreshConfig();
    } catch (e) {
      setError(
        e instanceof ApiError && e.message ? e.message : "That could not be saved.",
      );
    } finally {
      setBusy(false);
    }
  }, [busy, enabled, kind, baseUrl, owner, token, editingToken, refreshConfig]);

  const clearToken = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      // `null` CLEARS, where "" preserves. The three-way contract is the route's, and it is worth
      // a dedicated control: an operator who wants the credential gone should not have to guess
      // which empty value means which.
      await api.setPrefs({ forge: { token: null } });
      tokenRef.current = "";
      setToken("");
      setEditingToken(false);
      refreshConfig();
    } catch (e) {
      setError(
        e instanceof ApiError && e.message ? e.message : "That could not be saved.",
      );
    } finally {
      setBusy(false);
    }
  }, [refreshConfig]);

  return (
    <section className={styles.section} aria-labelledby="forge-h">
      <h2 id="forge-h">Forge connection</h2>
      <p className={styles.hint}>
        Where mission objectives are checked: is there a PR, are its checks green, has it been
        reviewed, is it merged. Read-only — nothing here can write to your forge. The access token
        is stored server-side and never sent to the browser.
      </p>
      {error && <p className={styles.err} role="alert" data-testid="forge-error">{error}</p>}
      {saved && !error ? (
        <p className={styles.hint} data-testid="forge-saved">
          Saved.
        </p>
      ) : null}
      {!block?.configured ? (
        <p className={styles.hint} data-testid="forge-unconfigured">
          Not configured — forge objectives will read as <strong>unknown</strong> rather than
          failed. An objective nobody could check has not been shown to be unmet.
        </p>
      ) : null}

      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
          data-testid="forge-enabled"
        />
        <span>Check mission objectives against a forge</span>
      </label>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="forge-kind">
          Forge
        </label>
        <select
          id="forge-kind"
          className={styles.aiInput}
          value={kind}
          onChange={(e) => setKind(e.target.value)}
          data-testid="forge-kind"
        >
          {KINDS.map((k) => (
            <option key={k.id} value={k.id}>
              {k.label}
            </option>
          ))}
        </select>
      </div>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="forge-base-url">
          API base URL
        </label>
        <input
          id="forge-base-url"
          className={styles.aiInput}
          type="url"
          spellCheck={false}
          placeholder="https://git.example.io"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          data-testid="forge-base-url"
        />
      </div>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="forge-owner">
          Default owner or organisation
        </label>
        <input
          id="forge-owner"
          className={styles.aiInput}
          spellCheck={false}
          placeholder="acme"
          value={owner}
          onChange={(e) => setOwner(e.target.value)}
          data-testid="forge-owner"
        />
        <p className={styles.hint}>
          Used when an objective names a bare repository. A mission whose folder is a git checkout
          can also resolve its own repository from the <code>origin</code> remote.
        </p>
      </div>

      <div className={styles.aiField}>
        <label
          className={styles.aiFieldLabel}
          {...(editingToken ? { htmlFor: "forge-token" } : {})}
        >
          Access token
          {block?.token_set ? (
            <span className={styles.aiKeyBadge} data-testid="forge-token-set">
              set
            </span>
          ) : null}
        </label>
        <div className={styles.aiKeyRow}>
          {editingToken ? (
            <input
              id="forge-token"
              className={styles.aiInput}
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
              data-testid="forge-token"
            />
          ) : (
            <button
              type="button"
              className={styles.secBtn}
              onClick={() => setEditingToken(true)}
              data-testid="forge-token-edit"
            >
              {block?.token_set ? "Replace token" : "Add token"}
            </button>
          )}
          {block?.token_set && !editingToken ? (
            <button
              type="button"
              className={styles.secBtn}
              disabled={busy}
              onClick={() => void clearToken()}
              data-testid="forge-token-clear"
            >
              Clear
            </button>
          ) : null}
        </div>
        <p className={styles.hint}>
          A <strong>read</strong> token is enough, and is what to use. A public forge needs none —
          the probes work without one.
        </p>
      </div>

      <button
        type="button"
        className={styles.secBtn}
        disabled={busy}
        onClick={() => void save()}
        data-testid="forge-save"
      >
        {busy ? "Saving…" : "Save connection"}
      </button>
    </section>
  );
}
