/** Settings → AI → HQ calls → "Speech on this machine" (#1397 P4).
 *
 *  `agent-sessions voice setup` installs a speech server on the machine BattleLab runs on. This
 *  block says where that stands — not set up, setup failed, stopped, running — and gives the one
 *  command for the next step. **Use this machine** points both speech sides at it SERVER-side: the
 *  server reads the access key from its own file, so the key never reaches this page and is never
 *  asked for. Copy puts the literal command on the clipboard; wrapping on a narrow screen is
 *  visual only. */
import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "../lib/api";
import type { ConformanceReport, VoiceLocal, VoiceSettings } from "../types/voice";
import { ConformanceReportView } from "./HqCallsReport";
import h from "./HqCallsSettings.module.css";
import styles from "./Settings.module.css";

const SETUP = "agent-sessions voice setup";
const START_UNIT = "systemctl --user start agent-sessions-speech";
const SERVE = "agent-sessions voice serve";
/** The models `voice/local/use` saves (`routes/voice.py`). */
const LOCAL_MODELS = { stt: "faster-whisper", tts: "kokoro-v1.0" };

type LocalState = "not_set_up" | "failed" | "stopped" | "running";

/** Which of the four states the status is in. A failed setup over a working one is still that
 *  working one (setup is transactional), with the failure named. */
function localState(st: VoiceLocal): LocalState {
  const installed = st.installed.server && st.token;
  if (!installed) return st.last_error ? "failed" : "not_set_up";
  return st.running ? "running" : "stopped";
}

function Command({ cmd, label, testId }: { cmd: string; label: string; testId: string }) {
  const [copied, setCopied] = useState<boolean | null>(null);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(cmd);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className={h.cmd} data-testid={testId}>
      <code className={h.cmdText} data-testid={`${testId}-text`}>
        {cmd}
      </code>
      <button
        type="button"
        className={styles.secBtnGhost}
        aria-label={label}
        onClick={() => void copy()}
        data-testid={`${testId}-copy`}
      >
        {copied ? "Copied" : "Copy"}
      </button>
      <span className="sr-only" role="status">
        {copied ? "Copied to the clipboard." : copied === false ? "Couldn’t copy: select the command instead." : ""}
      </span>
    </div>
  );
}

export function HqCallsLocal({
  switchToLocal,
}: {
  /** "Use this machine", through the settings page's single write queue (it writes the
   *  settings too); the page shows what it stored. */
  switchToLocal: () => Promise<{ settings: VoiceSettings; report: ConformanceReport }>;
}) {
  const [st, setSt] = useState<VoiceLocal | null>(null);
  const [failedRead, setFailedRead] = useState(false);
  const [busy, setBusy] = useState<"use" | "test" | null>(null);
  const [report, setReport] = useState<ConformanceReport | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(() => {
    api
      .voiceLocal()
      .then((x) => {
        setSt(x);
        setFailedRead(false);
      })
      .catch(() => setFailedRead(true));
  }, []);
  useEffect(load, [load]);

  if (failedRead && !st) {
    return (
      <section className={styles.section} aria-labelledby="hq-calls-local-h" data-testid="hq-calls-local">
        <h2 id="hq-calls-local-h">Speech on this machine</h2>
        <p className={styles.err} role="alert">
          Couldn’t read this machine’s speech setup.
        </p>
      </section>
    );
  }
  if (!st) return null;
  const state = localState(st);

  const use = async () => {
    setBusy("use");
    setMsg(null);
    setReport(null);
    try {
      const got = await switchToLocal();
      setReport(got.report);
      setMsg({ ok: true, text: "Both speech services now use this machine." });
    } catch (e) {
      const rec = e instanceof ApiError ? (e.record as { report?: ConformanceReport } | undefined) : undefined;
      if (rec?.report) setReport(rec.report);
      setMsg({ ok: false, text: e instanceof ApiError && e.message ? e.message : "Couldn’t use this machine." });
    } finally {
      setBusy(null);
      load();
    }
  };

  const test = async () => {
    setBusy("test");
    setMsg(null);
    setReport(null);
    try {
      setReport(
        await api.testVoice({
          stt: { base_url: st.url, model: LOCAL_MODELS.stt },
          tts: { base_url: st.url, model: LOCAL_MODELS.tts },
          voice: st.voices.woman,
        }),
      );
    } catch (e) {
      setMsg({ ok: false, text: e instanceof ApiError && e.message ? e.message : "The test could not run." });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section
      className={styles.section}
      aria-labelledby="hq-calls-local-h"
      data-testid="hq-calls-local"
      data-state={state}
    >
      <h2 id="hq-calls-local-h">Speech on this machine</h2>
      <p className={styles.hint}>
        Free and offline after one download. Or keep using your own speech server or OpenAI below —
        nothing changes for them.
      </p>
      {state === "not_set_up" ? (
        <div className={h.local}>
          <p className={h.localHead}>
            <span className="nj-stamp">Not set up</span>
            <b>Run this once on the machine BattleLab runs on</b>
          </p>
          <Command cmd={SETUP} label="Copy the setup command" testId="hq-calls-local-cmd" />
          <p className={styles.hint}>
            Downloads about 700 MB after you confirm. Checksums are verified; nothing starts until it's
            done.
          </p>
        </div>
      ) : state === "failed" ? (
        <div className={h.local}>
          <p className={h.localHead}>
            <span className="nj-stamp is-down">Setup failed</span>
            <b data-testid="hq-calls-local-error">{st.last_error}</b>
          </p>
          <p className={styles.hint}>
            Nothing was changed: your previous setup (if any) is still in use. Run the command again;
            the details are in <span className={h.mono}>voice/setup.log</span>.
          </p>
          <Command cmd={SETUP} label="Copy the setup command" testId="hq-calls-local-cmd" />
        </div>
      ) : state === "stopped" ? (
        <div className={h.local}>
          <p className={h.localHead}>
            <span className="nj-stamp is-warn">Stopped</span>
            <b>Installed, but the speech service isn't running</b>
          </p>
          <Command
            cmd={st.systemd ? START_UNIT : SERVE}
            label="Copy the start command"
            testId="hq-calls-local-cmd"
          />
          {st.systemd ? (
            <p className={styles.hint}>
              No user service manager on this machine? Run <span className={h.mono}>{SERVE}</span> in a
              terminal instead.
            </p>
          ) : (
            <p className={styles.hint}>This machine has no user service manager: keep that terminal open during calls.</p>
          )}
        </div>
      ) : (
        <div className={h.local}>
          <p className={h.localHead}>
            <span className="nj-stamp is-ok">Running</span>
            <b>Ready on this machine · 127.0.0.1 only</b>
            {st.in_use ? (
              <span className="nj-stamp is-hud" data-testid="hq-calls-local-in-use">
                In use
              </span>
            ) : null}
          </p>
          <dl className={h.facts}>
            <div>
              <dt>Hears with</dt>
              <dd>faster-whisper{st.stt ? ` ${st.stt}` : ""}</dd>
            </div>
            <div>
              <dt>Speaks with</dt>
              <dd>Kokoro v1.0</dd>
            </div>
            <div>
              <dt>Voices</dt>
              <dd>
                {st.voices.woman} · {st.voices.man}
              </dd>
            </div>
            <div>
              <dt>Budget</dt>
              <dd>{st.limits === "soft" ? "Soft limits" : "2 CPUs · 3 GB"}</dd>
            </div>
          </dl>
          <div className={styles.cardActions}>
            <button
              type="button"
              className={styles.secBtn}
              onClick={() => void use()}
              disabled={busy !== null}
              data-testid="hq-calls-local-use"
            >
              {busy === "use" ? "Checking…" : "Use this machine"}
            </button>
            {st.in_use ? (
              <button
                type="button"
                className={styles.secBtnGhost}
                onClick={() => void test()}
                disabled={busy !== null}
                data-testid="hq-calls-local-test"
              >
                {busy === "test" ? "Testing…" : "Test both"}
              </button>
            ) : null}
          </div>
          <p className={styles.hint}>
            Fills in both endpoints and the Woman/Man voices. The access key stays on the server; it's
            never shown here.
          </p>
          {st.limits === "soft" ? (
            <p className={styles.hint} data-testid="hq-calls-local-soft">
              Soft limits only: no memory or CPU cap without the user service.
            </p>
          ) : null}
        </div>
      )}
      {st.last_error && state !== "failed" ? (
        <p className={styles.hint} data-testid="hq-calls-local-last-error">
          The last setup failed ({st.last_error}); the working one above is still in use.
        </p>
      ) : null}
      {msg ? (
        <p
          className={msg.ok ? styles.hint : styles.err}
          role={msg.ok ? "status" : "alert"}
          data-testid="hq-calls-local-msg"
        >
          {msg.text}
        </p>
      ) : null}
      {report ? <ConformanceReportView report={report} testId="hq-calls-local-report" /> : null}
    </section>
  );
}
