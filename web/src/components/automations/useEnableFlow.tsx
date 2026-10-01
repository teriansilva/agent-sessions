/** Enabling an automation (#1201 §2): the consent dialog, then `POST …/enable` with the digest the
 *  operator was SHOWN. Shared by the list, run history and the editor, so every way in asks the
 *  same question in the same words.
 *
 *  A 409 that carries a fresh scope re-opens the dialog on those lines, unticked. A 409 without
 *  one is a stale revision: the automation is read again and the dialog shows it as it is now —
 *  retrying the old revision could only fail the same way. */
import { useCallback, useState } from "react";

import { ApiError, api } from "../../lib/api";
import {
  SCOPE_MOVED_NOTE,
  consentForEnable,
  consentFromError,
  errorWords,
  reconsent,
} from "../../lib/automations";
import type { Automation, ConsentRequired } from "../../types/automations";
import { ConsentDialog } from "./ConsentDialog";

const NO_SCOPE: ConsentRequired = { detail: "", widened: [], scope_lines: [] };
const NO_SCOPE_WORDS = "Its inputs can’t be checked right now, so there is nothing to approve yet.";

export function useEnableFlow(onDone: (a: Automation | null) => void) {
  const [target, setTarget] = useState<Automation | null>(null);
  const [consent, setConsent] = useState<ConsentRequired | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [returnTo, setReturnTo] = useState<HTMLElement | null>(null);

  const open = useCallback((a: Automation) => {
    setReturnTo(document.activeElement as HTMLElement | null);
    setTarget(a);
    // With no scope (its inputs could not be checked right now) the dialog still opens, says so,
    // and offers nothing to confirm.
    setConsent(consentForEnable(a) ?? NO_SCOPE);
    setError(a.scope_digest ? null : NO_SCOPE_WORDS);
  }, []);

  const close = useCallback(() => {
    setTarget(null);
    setConsent(null);
    setError(null);
  }, []);

  const confirm = useCallback(
    async (digest: string) => {
      if (!target) return;
      setBusy(true);
      setError(null);
      try {
        const a = await api.enableAutomation(target.id, {
          revision: target.revision,
          consent: true,
          scope_digest: digest,
        });
        close();
        onDone(a);
      } catch (e) {
        const again = consentFromError(e);
        if (again) {
          setConsent((prev) => reconsent(again, prev));
          setError(SCOPE_MOVED_NOTE);
        } else if (e instanceof ApiError && e.status === 409) {
          // A stale revision: read it again and ask about what it is NOW.
          try {
            const fresh = await api.automation(target.id);
            setTarget(fresh);
            setConsent(consentForEnable(fresh) ?? NO_SCOPE);
            setError("It changed since you opened this. This is how it looks now.");
          } catch (e2) {
            setError(errorWords(e2, "Couldn’t read it again."));
          }
          onDone(null);
        } else {
          setError(errorWords(e, "Couldn’t enable it."));
        }
      } finally {
        setBusy(false);
      }
    },
    [target, close, onDone],
  );

  const dialog =
    target && consent ? (
      <ConsentDialog
        name={target.name}
        mode="enable"
        consent={consent}
        busy={busy}
        error={error}
        reason={target.needs_reapproval ? target.reapproval_reason : undefined}
        onCancel={close}
        onConfirm={(d) => void confirm(d)}
        returnFocusTo={returnTo}
      />
    ) : null;

  return { open, dialog };
}
