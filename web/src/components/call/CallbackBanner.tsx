/** "Call me back" on an API session's pane (#2341 §4): one line where the background-jobs strip
 *  sits, "Calls you back when this turn finishes · Cancel", while a call-back waits on this
 *  session. Read when the pane opens, when a call ends (that is when one is recorded) and every
 *  `POLL_MS`; Cancel removes it on the server and nothing fires after. Nothing while none waits. */
import { PhoneCall } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api } from "../../lib/api";
import b from "../ui/actionButton.module.css";
import { useCall } from "./callContext";
import s from "./call.module.css";

const POLL_MS = 30_000;

export function CallbackBanner({ sessionKey }: { sessionKey: string }) {
  const { snap } = useCall();
  const [id, setId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const read = useCallback(() => {
    Promise.resolve()
      .then(() => api.voiceCallbacks(sessionKey))
      .then((r) => setId(r.callbacks.find((c) => c.key === sessionKey)?.id ?? null))
      .catch(() => undefined);
  }, [sessionKey]);
  useEffect(() => {
    read();
    const t = window.setInterval(read, POLL_MS);
    return () => window.clearInterval(t);
  }, [read]);
  useEffect(() => {
    if (snap.phase === "ended") read();
  }, [snap.phase, read]);
  if (!id) return null;
  const cancel = () => {
    setBusy(true);
    api
      .cancelVoiceCallback(id)
      .catch(() => undefined)
      .finally(() => {
        setBusy(false);
        read();
      });
  };
  return (
    <div className={s.callback} role="status" data-testid="structured-callback">
      <PhoneCall size={14} aria-hidden="true" />
      <span className={s.callbackText}>Calls you back when this turn finishes</span>
      <button
        type="button"
        className={`${b.ghost} ${b.compact} ${s.callbackCancel}`}
        onClick={cancel}
        disabled={busy}
        data-testid="structured-callback-cancel"
      >
        Cancel
      </button>
    </div>
  );
}
