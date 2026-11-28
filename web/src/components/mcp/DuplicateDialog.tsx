/** Server page ⋯ → Duplicate… (#2430): a copy of this server under a new name, for a second login
 *  in the same project. One row per DISTINCT credential variable the original names: keep it, pick
 *  another Variables secret (the wizard's "Existing variable"), or store a NEW one (write-only,
 *  through `POST /api/variables`, before the duplicate). The route copies source, origin, install,
 *  arguments and probe from the stored record; the copy is enabled in no project and needs its own
 *  check, so Duplicate lands on its Projects tab.
 *
 *  Retries are safe: a stored credential becomes an existing pick (never stored twice), and the
 *  dialog sends one `request_id` for its whole life, so a lost response returns the copy already
 *  made. Cancel never deletes a stored credential; it says it stays in Variables. */
import { useEffect, useMemo, useRef, useState } from "react";
import { Info, Lock } from "lucide-react";

import { ApiError, api } from "../../lib/api";
import type { McpServerSummary } from "../../types/mcp";
import { Field } from "../hud/Controls";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import { errorText, useSecrets } from "./mcpData";
import {
  credentialGroups,
  defaultRow,
  duplicateBody,
  duplicatedWords,
  hasErrors,
  nextFreeName,
  refusalPlace,
  remap,
  staysWords,
  storeNewSecrets,
  validateDuplicate,
  type DupErrors,
  type DupRow,
} from "./duplicateModel";
import styles from "./mcp.module.css";

const NO_ERRORS: DupErrors = { rows: {} };

export function DuplicateDialog({
  server,
  onCancel,
  onDuplicated,
  onStale,
}: {
  server: McpServerSummary;
  /** Closed without a copy; `note` says which stored credentials stay in Variables. */
  onCancel: (note: string) => void;
  onDuplicated: (copy: McpServerSummary, flash: string) => void;
  /** The original changed since it was read (409): the page reads it again. */
  onStale: () => void;
}) {
  const secretsState = useSecrets();
  const [requestId] = useState(() => crypto.randomUUID());
  const [taken, setTaken] = useState<ReadonlySet<string>>(new Set([server.name]));
  const [name, setName] = useState(() => nextFreeName(server.name, [server.name]));
  const nameTouched = useRef(false);
  const [label, setLabel] = useState("");
  const [edited, setEdited] = useState<Record<string, DupRow>>({});
  const [created, setCreated] = useState<string[]>([]);
  const [errors, setErrors] = useState<DupErrors>(NO_ERRORS);
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);

  // The names already used: the copy's suggestion skips them, and a clash is caught before
  // anything is stored. A failed read leaves only this server's name; the route still refuses.
  useEffect(() => {
    let live = true;
    api.mcpServers().then(
      (r) => {
        if (!live) return;
        const names = new Set([server.name, ...r.servers.map((x) => x.name)]);
        setTaken((prev) => new Set([...prev, ...names]));
        if (!nameTouched.current) setName(nextFreeName(server.name, names));
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [server.name]);

  const groups = useMemo(() => credentialGroups(server.settings), [server.settings]);
  const plain = server.settings.filter((x) => x.kind === "plain");
  const secrets = secretsState.secrets;
  const libraryEmpty = secrets !== null && secrets.length === 0 && created.length === 0;
  const rows = useMemo(() => {
    const out: Record<string, DupRow> = {};
    for (const g of groups) out[g.variable] = edited[g.variable] ?? defaultRow(g, secretsState.names, libraryEmpty);
    return out;
  }, [groups, edited, secretsState.names, libraryEmpty]);
  const patch = (variable: string, next: Partial<DupRow>) =>
    setEdited((prev) => ({ ...prev, [variable]: { ...rows[variable], ...next } }));

  const submit = async () => {
    const errs = validateDuplicate({ name: name.trim(), takenNames: taken, groups, rows, secretNames: secretsState.names });
    setErrors(errs);
    setFormError("");
    if (hasErrors(errs)) return;
    setBusy(true);
    try {
      const stored = await storeNewSecrets(
        groups,
        rows,
        (n, value) => api.createVariable({ name: n, kind: "secret", definition: { value_type: "text" }, value }),
        errorText,
      );
      setEdited((prev) => ({ ...prev, ...stored.rows }));
      if (stored.created.length) {
        setCreated((prev) => [...prev, ...stored.created]);
        secretsState.remember(stored.created); // a retry sees them as existing secrets
      }
      if (stored.failed) {
        setErrors({ rows: { [stored.failed.variable]: { variable: stored.failed.message } } });
        return;
      }
      const body = duplicateBody({ name, label, groups, rows: stored.rows, expected: server.updated_at, requestId });
      const copy = await api.duplicateMcpServer(server.id, body);
      onDuplicated(copy, duplicatedWords(server.name, copy.name, remap(groups, stored.rows)));
    } catch (e) {
      const message = errorText(e);
      const status = e instanceof ApiError ? e.status : 0;
      if (status === 409) {
        setFormError(
          /changed/.test(message)
            ? "This server changed since the dialog opened, so nothing was copied. Its page was read again: check the choices and press Duplicate again."
            : message,
        );
        onStale();
        return;
      }
      const place = refusalPlace(message, groups, rows);
      if (place.field === "name") {
        if (/exists/.test(message)) setTaken((prev) => new Set([...prev, name.trim()]));
        setErrors({ rows: {}, name: /exists/.test(message) ? `A server named ${name.trim()} exists. Pick another name.` : message });
      } else if (place.field === "row") setErrors({ rows: { [place.variable]: { variable: message } } });
      else setFormError(message);
    } finally {
      setBusy(false);
    }
  };

  const stays = staysWords(created);
  return (
    <ConfirmDialog
      tag="Duplicate MCP server"
      title={`Duplicate ${server.label || server.name}`}
      confirmLabel={busy ? "Duplicating…" : "Duplicate"}
      busy={busy}
      onCancel={() => onCancel(stays)}
      onConfirm={() => void submit()}
      markers={{ dialog: { "data-testid": "mcp-duplicate-dialog" }, confirm: { "data-testid": "mcp-duplicate-go" } }}
    >
      <div className={styles.dupForm}>
        <p className={styles.note}>
          A copy of <code>{server.name}</code> under its own name, for a second login in the same project. Its source,
          install, arguments and probe call come with it.
        </p>
        <Field
          label="Name"
          mono
          value={name}
          autoComplete="off"
          spellCheck={false}
          disabled={busy}
          onChange={(e) => {
            setName(e.target.value);
            nameTouched.current = true;
          }}
          hint={errors.name ? undefined : <>Agents see its tools under <code>{name.trim() || "…"}</code>.</>}
          error={errors.name}
        />
        <Field
          label="Label (optional)"
          value={label}
          placeholder={`${server.label || server.name} (second login)`}
          disabled={busy}
          onChange={(e) => setLabel(e.target.value)}
        />
        {secretsState.error && <p className={styles.error}>Variables can’t be read: {secretsState.error}</p>}
        {groups.length === 0 ? (
          <p className={styles.note}>It names no credentials: there is nothing to pick.</p>
        ) : (
          <div className={styles.dupCreds}>
            <p className="nj-label">Credentials · {groups.length}</p>
            {libraryEmpty && (
              <p className={styles.note} data-testid="mcp-dup-empty-library">
                Variables has no secrets yet, so there is nothing to pick: store one here with New credential.
              </p>
            )}
            {groups.map((g) => {
              const r = rows[g.variable];
              const e = errors.rows[g.variable] ?? {};
              const others = (secrets ?? []).filter((x) => x.name !== g.variable);
              const how = g.deliver === "env" ? "env variable" : "HTTP header";
              return (
                <div key={g.variable} className={styles.dupCred} data-testid="mcp-dup-credential" data-variable={g.variable}>
                  <p className={styles.dupKeys}>
                    {g.keys.map((k) => (
                      <code key={k}>{k}</code>
                    ))}
                    <span className={styles.note}>
                      {g.keys.length > 1 ? `${how}s` : how} · {server.name} reads <code>{g.variable}</code>
                    </span>
                  </p>
                  <div className={styles.pair}>
                    <select
                      className="nj-field"
                      aria-label={`Credential source for ${g.variable}`}
                      value={r.mode}
                      disabled={busy}
                      onChange={(ev) => patch(g.variable, { mode: ev.target.value as DupRow["mode"], secret: "" })}
                    >
                      <option value="existing">Existing variable</option>
                      <option value="new">New credential</option>
                    </select>
                    {r.mode === "existing" ? (
                      <select
                        className="nj-field is-mono"
                        aria-label={`Variable for ${g.variable}`}
                        value={r.target}
                        disabled={busy}
                        aria-invalid={e.variable ? true : undefined}
                        onChange={(ev) => patch(g.variable, { target: ev.target.value })}
                      >
                        <option value={g.variable}>
                          {g.variable} (same as {server.name})
                        </option>
                        {others.map((x) => (
                          <option key={x.name} value={x.name}>
                            {x.name}
                          </option>
                        ))}
                        {r.target !== g.variable && !others.some((x) => x.name === r.target) && (
                          <option value={r.target}>
                            {r.target}
                            {created.includes(r.target) ? " (just stored)" : " (not in Variables)"}
                          </option>
                        )}
                        {secrets !== null && others.length === 0 && (
                          <option value="" disabled>
                            No other secrets in Variables
                          </option>
                        )}
                      </select>
                    ) : (
                      <input
                        className="nj-field is-mono"
                        type="password"
                        autoComplete="new-password"
                        spellCheck={false}
                        aria-label={`New credential value for ${g.variable}`}
                        placeholder="Value (write-only)"
                        value={r.secret}
                        disabled={busy}
                        aria-invalid={e.value ? true : undefined}
                        onChange={(ev) => patch(g.variable, { secret: ev.target.value })}
                      />
                    )}
                  </div>
                  {e.value && <p className={styles.rowError}>{e.value}</p>}
                  {r.mode === "new" && (
                    <input
                      className="nj-field is-mono"
                      aria-label={`Variable name for ${g.variable}`}
                      value={r.newName}
                      disabled={busy}
                      autoComplete="off"
                      spellCheck={false}
                      aria-invalid={e.variable ? true : undefined}
                      onChange={(ev) => patch(g.variable, { newName: ev.target.value })}
                    />
                  )}
                  {e.variable && (
                    <p className={styles.rowError} role="alert">
                      {e.variable}
                    </p>
                  )}
                  <p className={styles.lock}>
                    <Lock size={12} aria-hidden="true" />
                    {r.mode === "new"
                      ? "Stored in Variables as a secret before the copy is made, never shown again."
                      : "Read from Variables when a session starts."}
                  </p>
                </div>
              );
            })}
          </div>
        )}
        {plain.length > 0 && (
          <p className={styles.note}>
            Plain settings are copied as they are (
            {plain.map((p, i) => (
              <span key={p.key}>
                {i > 0 && ", "}
                <code>{p.key}</code>
              </span>
            ))}
            ); edit them on the copy’s Settings tab.
          </p>
        )}
        <div className={styles.caution} role="note">
          <Info size={18} aria-hidden="true" />
          <div>The copy is enabled in no project and needs its own check.</div>
        </div>
        {stays && (
          <p className={styles.status} role="status" data-testid="mcp-dup-stored">
            Stored in Variables: {created.join(", ")}. {created.length === 1 ? "It stays" : "They stay"} there if you
            cancel.
          </p>
        )}
        {formError && (
          <p className={styles.error} role="alert" data-testid="mcp-dup-error">
            {formError}
          </p>
        )}
      </div>
    </ConfirmDialog>
  );
}
