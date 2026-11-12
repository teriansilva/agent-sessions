/** Settings → System → Artifacts (#1932 P2, board 4 F, decision 9): the global switch, which agents
 *  can take the tool, the limits and the store's usage, and where the public links are.
 *
 *  The limits are the server's (`artifact_store` caps, set by the host's environment), so they are
 *  read here, not edited: `PUT /api/artifacts/settings` takes only `{enabled}`. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { isActive, useEngineRoster } from "../../app/engineRoster";
import { api } from "../../lib/api";
import { ARTIFACTS_PATH } from "../../lib/routes";
import type { ArtifactSettings } from "../../types/artifacts";
import { Callsign, Panel } from "../hud/Panel";
import { Switch } from "../hud/Controls";
import { formatSize, hasToolChannel } from "./artifactModel";
import { StoreGauge } from "./StoreGauge";
import styles from "./artifacts.module.css";

const message = (e: unknown) =>
  e instanceof Error ? e.message : "The request failed. Try again.";

export function ArtifactsSettings() {
  const roster = useEngineRoster();
  const [s, setS] = useState<ArtifactSettings | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .artifactSettings()
      .then((v) => live && setS(v))
      .catch((e) => live && setError(message(e)));
    return () => {
      live = false;
    };
  }, []);

  const toggle = async (enabled: boolean) => {
    if (!s || busy) return;
    setBusy(true);
    setError("");
    try {
      const r = await api.setArtifactsEnabled(enabled);
      setS({ ...s, enabled: r.enabled });
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  // Agents that run a process (a `chat` agent runs none), named by their manifest label. An API
  // client is a mode of its source agent (#1408), so it is not listed on its own.
  const agents = roster.engines.filter(
    (e) =>
      e.kind === "agent" &&
      e.runtime !== "chat" &&
      e.runtime !== "api" &&
      isActive(e),
  );
  const apiWithTool = roster.engines.some(
    (e) => e.runtime === "api" && isActive(e) && hasToolChannel(e) === true,
  );
  const withTool = agents
    .filter((e) => hasToolChannel(e) === true)
    .map((e) => e.label);
  const without = agents
    .filter((e) => hasToolChannel(e) === false)
    .map((e) => e.label);

  if (!s)
    return (
      <p
        className={styles.status}
        role="status"
        data-testid="artifacts-settings-loading"
      >
        {error || "Reading the artifact store…"}
      </p>
    );
  return (
    <div className={styles.settings} data-testid="artifacts-settings">
      <Panel faceClassName={styles.face} tone="quiet" rail="short">
        <Callsign title="Artifacts" value={s.enabled ? "On" : "Off"} />
        <div className={styles.setRow}>
          <div className={styles.setLbl}>
            <b id="artifacts-switch-label">Let agents publish artifacts</b>
            <span>
              Off by default. Turning it on offers the tool nowhere yet: you
              choose per session, project or mission. Turning it off hides the
              tool from the next launch; published artifacts stay.
            </span>
          </div>
          <Switch
            checked={s.enabled}
            onChange={(v) => void toggle(v)}
            label="Let agents publish artifacts"
            disabled={busy}
            testId="artifacts-switch"
          />
        </div>
        {(withTool.length > 0 || without.length > 0) && (
          <dl className={styles.kv}>
            <dt>Agents</dt>
            <dd>
              {withTool.length > 0
                ? `${withTool.join(", ")}${apiWithTool ? " and their API runtimes" : ""} can take the tool.`
                : "No agent here can take the tool."}
              {without.length > 0 &&
                ` ${without.join(", ")} ${without.length === 1 ? "has" : "have"} no tool channel.`}
            </dd>
          </dl>
        )}
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
      </Panel>

      <Panel faceClassName={styles.face} tone="quiet" rail="none">
        <Callsign title="Limits" value="this box" />
        <p className={styles.note}>
          A publish over a limit is refused, and the agent hears which limit.
        </p>
        <div className={styles.usage}>
          <span className={styles.meta}>
            <b>{formatSize(s.bytes)}</b> of {formatSize(s.caps.total_bytes)}{" "}
            used
            <span className={styles.dot}>
              {s.artifacts} {s.artifacts === 1 ? "artifact" : "artifacts"}
            </span>
          </span>
          <StoreGauge
            bytes={s.bytes}
            total={s.caps.total_bytes}
            segments={24}
          />
        </div>
        <dl className={styles.caps}>
          <div>
            <dt>Size per revision</dt>
            <dd>{formatSize(s.caps.revision_bytes)}</dd>
          </div>
          <div>
            <dt>Files per revision</dt>
            <dd>
              {s.caps.revision_files.toLocaleString("en-US")}
              <small>files</small>
            </dd>
          </div>
          <div>
            <dt>Revisions kept per artifact</dt>
            <dd>
              {s.caps.revisions}
              <small>revisions</small>
            </dd>
            <p>The oldest are pruned; the latest never is.</p>
          </div>
          <div>
            <dt>Total</dt>
            <dd>{formatSize(s.caps.total_bytes)}</dd>
            <p>At the total, publishing stops until you delete artifacts.</p>
          </div>
        </dl>
      </Panel>

      <Panel faceClassName={styles.face} tone="quiet" rail="none">
        <Callsign title="Public links" value={`${s.public} live`} />
        <p className={styles.note}>
          Every public artifact is in{" "}
          <Link
            className={styles.link}
            to={`${ARTIFACTS_PATH}?visibility=public`}
          >
            Library → Artifacts
          </Link>
          , filtered to Public, with Revoke. Credential-shaped text files refuse
          the publish, so they never reach a link.
        </p>
      </Panel>
    </div>
  );
}
