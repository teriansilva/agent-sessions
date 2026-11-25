/** Library → MCP servers (#2163 P1c): the settings & credentials table — the wizard's Settings step
 *  and a server page's Settings tab. A plain setting holds its value (stored with the server). A
 *  credential is a Variables secret: either a NEW one, typed into a write-only field that is never
 *  read back, or an EXISTING one picked by name. "Delivered as" says how the server receives it: an
 *  environment variable for a package (stdio), an HTTP header for a remote server, optionally with a
 *  prefix such as `Bearer `. */
import { KeyRound, Lock, Plus, Trash2, Braces } from "lucide-react";

import type { SharedVariable } from "../../types/variables";
import { Seg } from "../hud/Controls";
import {
  blankRow,
  deliveredAs,
  variableNameFor,
  type RowErrors,
  type SettingRow,
} from "./mcpModel";
import styles from "./mcp.module.css";

export function SettingsEditor({
  rows,
  onChange,
  transport,
  serverName,
  secrets,
  errors,
  disabled = false,
}: {
  rows: SettingRow[];
  onChange: (rows: SettingRow[]) => void;
  transport: "stdio" | "http";
  serverName: string;
  secrets: SharedVariable[] | null;
  errors: Record<string, RowErrors>;
  disabled?: boolean;
}) {
  const patch = (rid: string, next: Partial<SettingRow>) =>
    onChange(
      rows.map((r) => {
        if (r.rid !== rid) return r;
        const merged = { ...r, ...next };
        // A new credential's variable name follows its key while it is still the suggested one.
        if (
          "key" in next &&
          merged.kind === "credential" &&
          merged.credMode === "new" &&
          r.variable === variableNameFor(serverName, r.key || "credential")
        )
          merged.variable = variableNameFor(serverName, merged.key || "credential");
        if ("kind" in next && next.kind === "credential" && !merged.variable)
          merged.variable = variableNameFor(serverName, merged.key || "credential");
        return merged;
      }),
    );
  const keyHint = transport === "stdio" ? "e.g. API_TOKEN" : "e.g. Authorization";
  return (
    <div className={styles.valueCell}>
      {rows.length > 0 && (
        <table className={styles.tbl} data-testid="mcp-settings-table">
          <thead>
            <tr>
              <th scope="col">Setting</th>
              <th scope="col">Value</th>
              <th scope="col">Delivered as</th>
              <th scope="col">
                <span className="nj-sr-only">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const e = errors[r.rid] ?? {};
              const name = r.key.trim() || `setting ${i + 1}`;
              const d = deliveredAs(r, transport);
              return (
                <tr key={r.rid} data-testid="mcp-setting-row" data-kind={r.kind}>
                  <td data-label="Setting">
                    <div className={styles.keyCell}>
                      <input
                        className="nj-field is-mono"
                        aria-label={`Setting ${i + 1} name`}
                        placeholder={keyHint}
                        value={r.key}
                        disabled={disabled}
                        aria-invalid={e.key ? true : undefined}
                        onChange={(ev) => patch(r.rid, { key: ev.target.value })}
                      />
                      {e.key && <p className={styles.rowError}>{e.key}</p>}
                      <Seg<SettingRow["kind"]>
                        label={`${name}: plain or credential`}
                        value={r.kind}
                        onChange={(kind) => patch(r.rid, { kind })}
                        options={[
                          {
                            value: "plain",
                            label: (
                              <>
                                <Braces size={12} aria-hidden="true" /> Plain
                              </>
                            ),
                            disabled,
                          },
                          {
                            value: "credential",
                            label: (
                              <>
                                <KeyRound size={12} aria-hidden="true" /> Credential
                              </>
                            ),
                            disabled,
                          },
                        ]}
                      />
                    </div>
                  </td>
                  <td data-label="Value">
                    {r.kind === "plain" ? (
                      <input
                        className="nj-field is-mono"
                        aria-label={`Value of ${name}`}
                        value={r.value}
                        disabled={disabled}
                        onChange={(ev) => patch(r.rid, { value: ev.target.value })}
                      />
                    ) : (
                      <div className={styles.valueCell}>
                        <div className={styles.pair}>
                          <select
                            className="nj-field"
                            aria-label={`Credential source for ${name}`}
                            value={r.credMode}
                            disabled={disabled}
                            onChange={(ev) =>
                              patch(r.rid, {
                                credMode: ev.target.value as SettingRow["credMode"],
                                secret: "",
                                variable:
                                  ev.target.value === "existing"
                                    ? ""
                                    : variableNameFor(serverName, r.key || "credential"),
                              })
                            }
                          >
                            <option value="new">New credential</option>
                            <option value="existing">Existing variable</option>
                          </select>
                          {r.credMode === "new" ? (
                            <input
                              className="nj-field is-mono"
                              type="password"
                              autoComplete="new-password"
                              spellCheck={false}
                              aria-label={`New credential value for ${name}`}
                              placeholder="Value (write-only)"
                              value={r.secret}
                              disabled={disabled}
                              aria-invalid={e.value ? true : undefined}
                              onChange={(ev) => patch(r.rid, { secret: ev.target.value })}
                            />
                          ) : (
                            <select
                              className="nj-field is-mono"
                              aria-label={`Variable for ${name}`}
                              value={r.variable}
                              disabled={disabled}
                              aria-invalid={e.variable ? true : undefined}
                              onChange={(ev) => patch(r.rid, { variable: ev.target.value })}
                            >
                              <option value="">Pick a secret…</option>
                              {(secrets ?? []).map((s) => (
                                <option key={s.name} value={s.name}>
                                  {s.name}
                                </option>
                              ))}
                              {r.variable && !(secrets ?? []).some((s) => s.name === r.variable) && (
                                <option value={r.variable}>{r.variable} (not in Variables)</option>
                              )}
                            </select>
                          )}
                        </div>
                        {e.value && <p className={styles.rowError}>{e.value}</p>}
                        {r.credMode === "new" && (
                          <input
                            className="nj-field is-mono"
                            aria-label={`Variable name for ${name}`}
                            value={r.variable}
                            disabled={disabled}
                            aria-invalid={e.variable ? true : undefined}
                            onChange={(ev) => patch(r.rid, { variable: ev.target.value })}
                          />
                        )}
                        {e.variable && <p className={styles.rowError}>{e.variable}</p>}
                        {transport === "http" && (
                          <input
                            className="nj-field is-mono"
                            aria-label={`Prefix for ${name}`}
                            placeholder="Prefix, e.g. Bearer (optional)"
                            value={r.prefix}
                            disabled={disabled}
                            onChange={(ev) => patch(r.rid, { prefix: ev.target.value })}
                          />
                        )}
                        <p className={styles.lock}>
                          <Lock size={12} aria-hidden="true" />
                          {r.credMode === "new"
                            ? "Stored in Variables as a secret, never shown again. Or pick Existing variable."
                            : "Read from Variables when a session starts."}
                        </p>
                      </div>
                    )}
                  </td>
                  <td data-label="Delivered as">
                    <span className={styles.deliv}>
                      <span>{d.how}</span>
                      <code>{d.what}</code>
                    </span>
                  </td>
                  <td>
                    <button
                      type="button"
                      className={styles.iconBtn}
                      aria-label={`Remove ${name}`}
                      disabled={disabled}
                      onClick={() => onChange(rows.filter((x) => x.rid !== r.rid))}
                    >
                      <Trash2 size={15} aria-hidden="true" />
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <div className={styles.btnRow}>
        <button
          type="button"
          className={styles.ghost}
          disabled={disabled}
          onClick={() => onChange([...rows, blankRow()])}
          data-testid="mcp-add-setting"
        >
          <Plus size={14} aria-hidden="true" />
          Add setting
        </button>
        {rows.length === 0 && (
          <span className={styles.note}>
            None yet. A server that needs no settings or credentials (a public remote one) checks as it is.
          </span>
        )}
      </div>
    </div>
  );
}
