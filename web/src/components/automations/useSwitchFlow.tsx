/** "Switch to API client" (#1728 Phase 2): the fresh-consent sheet naming the new client, its
 *  model and its permission mode; Confirm saves through the ordinary validated `PATCH`, and an
 *  enabled automation then gets the server's own consent dialog (its lines, its digest). The
 *  terminal's permission grant is never carried over. Shared by the list and the editor. */
import { useCallback, useState } from "react";

import { engineLabel } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import {
  SCOPE_MOVED_NOTE,
  consentFromError,
  errorWords,
  reconsent,
  switchBody,
} from "../../lib/automations";
import type { Automation, ConsentRequired } from "../../types/automations";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import { ConsentDialog } from "./ConsentDialog";
import styles from "./automations.module.css";

type Phase = { a: Automation; engine: string; consent: ConsentRequired | null };

/** The switch flow: the fresh-consent sheet, then (for an enabled automation) the server's consent
 *  dialog. `onDone` gets the saved automation. Shared by the list and the editor. */
export function useSwitchFlow(onDone: (a: Automation) => void) {
  const [phase, setPhase] = useState<Phase | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [returnTo, setReturnTo] = useState<HTMLElement | null>(null);

  const open = useCallback((a: Automation, engine: string) => {
    setReturnTo(document.activeElement as HTMLElement | null);
    setError(null);
    setPhase({ a, engine, consent: null });
  }, []);
  const close = useCallback(() => {
    setPhase(null);
    setError(null);
  }, []);

  const save = useCallback(
    async (digest?: string) => {
      if (!phase) return;
      const body = switchBody(phase.a, phase.engine);
      if (!body) {
        setError("Only an automation that starts a session can switch its agent.");
        return;
      }
      setBusy(true);
      setError(null);
      try {
        const a = await api.patchAutomation(phase.a.id, {
          ...body,
          ...(digest ? { consent: true, scope_digest: digest } : {}),
        });
        setPhase(null);
        onDone(a);
      } catch (e) {
        const need = consentFromError(e);
        if (need) {
          // An enabled automation: the server's own scope lines and digest, asked once more.
          setPhase((p) => (p ? { ...p, consent: digest ? reconsent(need, p.consent) : need } : p));
          setError(digest ? SCOPE_MOVED_NOTE : null);
        } else if (e instanceof ApiError && e.status === 409) {
          setError(`${e.message}. It changed since you opened this — close this and try again.`);
        } else {
          setError(errorWords(e, "Couldn’t switch it."));
        }
      } finally {
        setBusy(false);
      }
    },
    [phase, onDone],
  );

  let dialog = null;
  if (phase && phase.consent) {
    dialog = (
      <ConsentDialog
        name={phase.a.name}
        mode="save"
        consent={phase.consent}
        busy={busy}
        error={error}
        onCancel={close}
        onConfirm={(d) => void save(d)}
        returnFocusTo={returnTo}
      />
    );
  } else if (phase) {
    const from = phase.a.action?.kind === "start_session" ? phase.a.action.engine : "";
    dialog = (
      <ConfirmDialog
        tag="Switch to API client"
        title={`Switch “${phase.a.name}” to ${engineLabel(phase.engine)}?`}
        confirmLabel="Switch and confirm"
        busy={busy}
        onCancel={close}
        onConfirm={() => void save()}
        returnFocusTo={returnTo}
        markers={{
          dialog: { "data-testid": "automation-switch-sheet" },
          confirm: { "data-testid": "automation-switch-confirm" },
        }}
      >
        <p>
          Switching changes what this automation runs. Confirming saves it as a fresh consent: the
          terminal’s permission setting is not carried over.
        </p>
        <dl className={styles.switchFacts} data-testid="automation-switch-facts">
          <dt>Runs</dt>
          <dd>
            <s>{engineLabel(from)}</s> → <b>{engineLabel(phase.engine)}</b>
          </dd>
          <dt>Model</dt>
          <dd>the agent’s default</dd>
          <dt>Permissions</dt>
          <dd>asks before each request</dd>
        </dl>
        {phase.a.enabled && (
          <p>It is enabled, so you are asked to approve its new scope next.</p>
        )}
        {error && (
          <p className={styles.error} role="alert" data-testid="automation-switch-error">
            {error}
          </p>
        )}
      </ConfirmDialog>
    );
  }
  return { open, dialog };
}
