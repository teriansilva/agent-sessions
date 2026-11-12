/** Artifacts activation dialogs (#1932 P2, board 4): a session's, from its ⋯ menu, and a mission's,
 *  from the mission header's ⋯ menu. Both are the one OFF / PRIVATE / PUBLIC control.
 *
 *  **A change reaches the NEXT launch only** (the broker mints the tool where an agent process is
 *  born). So after a change the dialog re-reads what each running agent's next launch gets and,
 *  where that differs from what it had when the dialog opened, says so with the board's two
 *  wordings: Off → on "does not have the tool yet; it gets it at restart", Private ↔ Public "the
 *  new mode applies when the agent restarts". The notice sits on the neutral HUD edge, never a
 *  status colour (decision 4).
 *
 *  **There is no Restart now button.** The app has no action that restarts one session's agent
 *  in place (archive + unarchive relaunches, but it is not a restart and it is the operator's own
 *  act), so the notice states the fact and offers nothing it cannot do. */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Plug, RotateCcw } from "lucide-react";

import {
  engineInfo,
  engineName,
  useEngineRoster,
} from "../../app/engineRoster";
import { api } from "../../lib/api";
import { settingsPath } from "../../routes/settingsTabs";
import type { Mission, Session } from "../../types/api";
import type { ArtifactActivation, ArtifactMode } from "../../types/artifacts";
import { ActivationControl } from "./ActivationControl";
import { ArtifactDialog } from "./ArtifactDialog";
import {
  hasToolChannel,
  modeLabel,
  restartChange,
  restartLine,
  sourceWords,
  type RestartChange,
} from "./artifactModel";
import styles from "./artifacts.module.css";

const message = (e: unknown) =>
  e instanceof Error ? e.message : "The request failed. Try again.";

/** One running agent whose next launch now differs from what it had. */
interface Pending {
  key: string;
  engine: string;
  title: string;
  change: RestartChange;
}

/** `named`: say which session each line is about (a mission holds several). */
function RestartNotice({
  pending,
  named = false,
}: {
  pending: Pending[];
  named?: boolean;
}) {
  if (pending.length === 0) return null;
  return (
    <div
      className={styles.restart}
      role="status"
      data-testid="artifact-restart-notice"
    >
      <RotateCcw size={16} aria-hidden="true" />
      <p>
        Takes effect when the agent restarts.
        {pending.map((p) => (
          <small key={p.key} data-change={p.change}>
            {named ? `${p.title}: ` : ""}
            {restartLine(p.change, p.engine)}
          </small>
        ))}
      </p>
    </div>
  );
}

function GlobalOff() {
  return (
    <p className={`${styles.note} ${styles.spaced}`}>
      Artifacts are off for every agent in{" "}
      <Link className={styles.link} to={settingsPath("artifacts")}>
        Settings → Artifacts
      </Link>
      . A setting here takes effect once they are on.
    </p>
  );
}

export function SessionActivationDialog({
  session,
  onClose,
  returnFocusTo,
}: {
  /** At least the key and engine; the dialog reads the rest (cwd, running) when it is missing. */
  session: Pick<Session, "id" | "engine"> &
    Partial<Pick<Session, "title" | "cwd" | "running">>;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  useEngineRoster();
  const key = session.id;
  const [info, setInfo] = useState(session);
  const [act, setAct] = useState<ArtifactActivation | null>(null);
  const [before, setBefore] = useState<ArtifactMode | null>(null);
  const [pending, setPending] = useState<Pending[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    // No tool channel: there is no mode to read or set, only the reason to show.
    if (hasToolChannel(engineInfo(session.engine)) === false) return;
    let live = true;
    (async () => {
      let s = session;
      if (s.cwd === undefined || s.running === undefined) {
        s = { ...s, ...(await api.session(key).catch(() => ({}))) };
        if (live) setInfo(s);
      }
      const a = await api.artifactActivation("session", key, s.cwd);
      if (!live) return;
      setAct(a);
      setBefore(a.effective?.mode ?? null);
    })().catch((e) => live && setError(message(e)));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- read once per dialog
  }, [key]);

  const channel = hasToolChannel(engineInfo(session.engine));
  const set = async (mode: ArtifactMode | null) => {
    if (busy || !act) return;
    setBusy(true);
    setError("");
    try {
      await api.setArtifactActivation("session", key, mode);
      const next = await api.artifactActivation("session", key, info.cwd);
      setAct(next);
      const change =
        before && next.effective
          ? restartChange(before, next.effective.mode)
          : null;
      setPending(
        change && info.running
          ? [{ key, engine: session.engine, title: info.title ?? key, change }]
          : [],
      );
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  const eff = act?.effective;
  const source =
    act === null ? null : act.mode !== null ? (
      <>
        Set for this session
        {before && eff && before !== eff.mode ? (
          <>
            {" "}
            · was <b>{modeLabel(before)}</b>
          </>
        ) : null}
        . A session&apos;s own setting wins over its mission&apos;s, and a
        mission&apos;s over the project default.
      </>
    ) : eff ? (
      <>
        Not set for this session: it gets <b>{modeLabel(eff.mode)}</b>,{" "}
        {sourceWords(eff.source)}.
      </>
    ) : null;

  return (
    <ArtifactDialog
      tag="Artifacts"
      value="this session"
      onClose={onClose}
      returnFocusTo={returnFocusTo}
      testId="artifact-activation-session"
      actions={
        <button type="button" className={styles.ghost} onClick={onClose}>
          Close
        </button>
      }
    >
      <div className={styles.dmeta}>
        <span className={styles.eng}>{session.engine}</span>
        <span className={styles.ell}>{info.title ?? key}</span>
        {info.running && <span className={styles.dot}>running</span>}
      </div>
      {channel === false ? (
        <>
          <ActivationControl value={null} onChange={() => {}} disabled />
          <div
            className={`${styles.info} ${styles.spaced}`}
            data-testid="artifact-not-available"
          >
            <Plug size={16} aria-hidden="true" />
            <p>
              <b>Not available for this agent.</b> {engineName(session.engine)}{" "}
              has no BattleLab tool channel, so it cannot publish artifacts. Its
              files are still in the Files panel.
            </p>
          </div>
        </>
      ) : act === null ? (
        <p className={styles.status} role="status">
          {error || "Reading…"}
        </p>
      ) : (
        <>
          <ActivationControl
            value={act.mode}
            onChange={(m) => void set(m)}
            busy={busy}
            source={source}
            clearLabel="Clear: use the mission's or project's setting"
            testId="artifact-activation-control"
          />
          {!act.enabled && <GlobalOff />}
          <RestartNotice pending={pending} />
          {pending.length === 0 && (
            <p className={`${styles.note} ${styles.spaced}`}>
              The agent publishes with{" "}
              <span className={styles.mono}>artifact_publish</span>. Every
              artifact lands in <b>Library → Artifacts</b> and this
              session&apos;s Artifacts tab.
            </p>
          )}
          {error && (
            <p className={styles.error} role="alert">
              {error}
            </p>
          )}
        </>
      )}
    </ArtifactDialog>
  );
}

interface Held {
  key: string;
  engine: string;
  title: string;
  cwd?: string;
  running: boolean;
  mode: ArtifactMode | null;
}

/** At most this many held sessions are read for the restart notice: a mission holds a handful. */
const HELD_MAX = 12;

export function MissionActivationDialog({
  mission,
  onClose,
  returnFocusTo,
}: {
  mission: Pick<Mission, "id" | "title" | "sessions" | "cwd">;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const [act, setAct] = useState<ArtifactActivation | null>(null);
  const [held, setHeld] = useState<Held[]>([]);
  const [pending, setPending] = useState<Pending[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const keys = mission.sessions
    .filter((s) => !s.removed_at)
    .map((s) => s.session_key)
    .slice(0, HELD_MAX);

  const readHeld = useCallback(
    async (known?: Held[]): Promise<Held[]> =>
      Promise.all(
        keys.map(async (key) => {
          const s: Partial<Session> =
            known?.find((h) => h.key === key) ??
            (await api.session(key).catch(() => ({})));
          const cwd = s.cwd ?? mission.cwd ?? undefined;
          const a = await api
            .artifactActivation("session", key, cwd)
            .catch(() => null);
          return {
            key,
            engine: s.engine ?? key.split(":")[0],
            title: s.title ?? key,
            cwd,
            running: !!s.running,
            mode: a?.effective?.mode ?? null,
          };
        }),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the held list is read once per dialog
    [mission.id],
  );

  useEffect(() => {
    let live = true;
    Promise.all([api.artifactActivation("mission", mission.id), readHeld()])
      .then(([a, h]) => {
        if (!live) return;
        setAct(a);
        setHeld(h);
      })
      .catch((e) => live && setError(message(e)));
    return () => {
      live = false;
    };
  }, [mission.id, readHeld]);

  const set = async (mode: ArtifactMode | null) => {
    if (busy || !act) return;
    setBusy(true);
    setError("");
    try {
      await api.setArtifactActivation("mission", mission.id, mode);
      const [a, after] = await Promise.all([
        api.artifactActivation("mission", mission.id),
        readHeld(held),
      ]);
      setAct(a);
      const next: Pending[] = [];
      for (const h of after) {
        const was = held.find((x) => x.key === h.key)?.mode;
        const change = was && h.mode ? restartChange(was, h.mode) : null;
        if (change && h.running)
          next.push({ key: h.key, engine: h.engine, title: h.title, change });
      }
      setPending(next);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  const source =
    act === null ? null : act.mode !== null ? (
      <>
        This mission&apos;s setting wins over the project default; a
        session&apos;s own setting wins over it.
      </>
    ) : (
      <>Not set for this mission: its sessions follow the project default.</>
    );

  return (
    <ArtifactDialog
      tag="Artifacts"
      value="mission"
      onClose={onClose}
      returnFocusTo={returnFocusTo}
      testId="artifact-activation-mission"
      actions={
        <button type="button" className={styles.ghost} onClick={onClose}>
          Close
        </button>
      }
    >
      <div className={styles.dmeta}>
        <span className={styles.ell}>{mission.title}</span>
        <span className={styles.dot}>
          {keys.length} {keys.length === 1 ? "session" : "sessions"}
        </span>
      </div>
      {act === null ? (
        <p className={styles.status} role="status">
          {error || "Reading…"}
        </p>
      ) : (
        <>
          <ActivationControl
            value={act.mode}
            onChange={(m) => void set(m)}
            busy={busy}
            label="Artifacts for every session of this mission"
            source={source}
            clearLabel="Clear: use the project default"
            testId="artifact-activation-control"
          />
          {!act.enabled && <GlobalOff />}
          <RestartNotice pending={pending} named />
          <p className={`${styles.note} ${styles.spaced}`}>
            Each publish also lands on the mission shelf as a delivered link.
          </p>
          {error && (
            <p className={styles.error} role="alert">
              {error}
            </p>
          )}
        </>
      )}
    </ArtifactDialog>
  );
}
