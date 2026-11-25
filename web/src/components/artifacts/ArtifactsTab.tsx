/** A session's ARTIFACTS tab (#1932 P2, board 2, decision 2): the third tab of its Files panel, beside
 *  FILES and GIT — the per-session counterpart of the mission shelf, drawn as tiles. Its head says
 *  what this session's next launch gets (the tool's mode) and opens the activation dialog; a tile
 *  opens the artifact's page. An agent without a BattleLab tool channel says so instead (board 5 D).
 *
 *  Tiles show an image artifact's own pixels (the browser's `<img>` on the stable link, which the
 *  server answers with the view-token redirect). A site or another file is a card with its entry
 *  name: rendering every site live in a scaled frame would run each one's scripts just to draw a
 *  thumbnail, so the live preview stays on the artifact's page. */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AppWindow,
  ExternalLink,
  File,
  Package,
  Plug,
  Power,
  SlidersHorizontal,
} from "lucide-react";

import {
  engineInfo,
  engineName,
  useEngineRoster,
} from "../../app/engineRoster";
import { api } from "../../lib/api";
import { appUrl } from "../../lib/appBase";
import {
  ARTIFACTS_PATH,
  artifactPath,
  hostedArtifactPath,
} from "../../lib/routes";
import type { Artifact, ArtifactActivation } from "../../types/artifacts";
import { Loader } from "../hud/Loader";
import { KindTag, VisibilityTag } from "./ArtifactBits";
import { SessionActivationDialog } from "./ActivationDialog";
import {
  ago,
  filesLabel,
  formatSize,
  hasToolChannel,
  isImageEntry,
  modeLabel,
  revLabel,
} from "./artifactModel";
import styles from "./artifacts.module.css";

function Tile({ a }: { a: Artifact }) {
  const image = isImageEntry(a.kind, a.entry);
  return (
    <Link
      className={styles.tile}
      to={artifactPath(a.id)}
      aria-label={`${a.title}, ${a.visibility}`}
      data-artifact={a.id}
    >
      <div className={styles.media}>
        <span className={styles.mediaTag}>
          <KindTag kind={a.kind} entry={a.entry} />
        </span>
        {a.kind === "site" && a.entry && (
          <span className={styles.mediaPath}>/{a.entry}</span>
        )}
        {image ? (
          <img
            className={styles.thumb}
            src={appUrl(hostedArtifactPath(a.id))}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
          />
        ) : (
          <span className={styles.docCard} aria-hidden="true">
            {a.kind === "site" ? <AppWindow size={28} /> : <File size={28} />}
            <span>
              {a.kind === "site" ? filesLabel(a.files) : (a.entry ?? "file")}
            </span>
          </span>
        )}
      </div>
      <div className={styles.tbody}>
        <p className={styles.ttitle}>{a.title}</p>
        <p className={styles.twhy}>{a.why}</p>
        <span className={styles.meta}>
          <b>{revLabel(a.revision)}</b>
          {a.kind === "site" && (
            <span className={styles.dot}>{filesLabel(a.files)}</span>
          )}
          <span className={styles.dot}>{formatSize(a.bytes)}</span>
          <span className={styles.dot}>{ago(a.updated_at)}</span>
        </span>
      </div>
      <div className={styles.tfoot}>
        <VisibilityTag visibility={a.visibility} />
        <span>
          Open
          <ExternalLink size={12} aria-hidden="true" />
        </span>
      </div>
    </Link>
  );
}

export function ArtifactsTab({
  sessionKey,
  cwd,
}: {
  sessionKey: string;
  cwd: string;
}) {
  useEngineRoster();
  const engine = sessionKey.split(":")[0];
  const channel = hasToolChannel(engineInfo(engine));
  const [items, setItems] = useState<Artifact[] | null>(null);
  const [act, setAct] = useState<ArtifactActivation | null>(null);
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState<HTMLElement | null>(null);

  const load = useCallback(() => {
    let live = true;
    api
      .artifacts({ session: sessionKey })
      .then((p) => live && setItems(p.items))
      .catch(
        (e) =>
          live &&
          setError(
            e instanceof Error ? e.message : "Couldn’t read the artifacts.",
          ),
      );
    api
      .artifactActivation("session", sessionKey, cwd)
      .then((a) => live && setAct(a))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [sessionKey, cwd]);
  useEffect(() => load(), [load]);

  const foot = (
    <div className={styles.tabFoot}>
      <Package size={14} aria-hidden="true" />
      <span>
        Every artifact from every session:{" "}
        <Link className={styles.link} to={ARTIFACTS_PATH}>
          Library → Artifacts
        </Link>
      </span>
    </div>
  );

  if (channel === false)
    return (
      <div className={styles.tab} data-testid="artifacts-tab">
        <div className={styles.info} data-testid="artifact-not-available">
          <Plug size={16} aria-hidden="true" />
          <p>
            <b>Not available for this agent.</b> {engineName(engine)} has no
            BattleLab tool channel, so this session cannot publish artifacts.
            Use the Files tab to open what it wrote.
          </p>
        </div>
        {foot}
      </div>
    );

  const mode = act?.effective?.mode;
  const pub = items?.filter((a) => a.visibility === "public").length ?? 0;
  const inMission = items?.some((a) => a.origin.mission_id) ?? false;
  return (
    <div className={styles.tab} data-testid="artifacts-tab">
      {mode === "off" && items !== null && items.length === 0 ? (
        <div className={styles.info} data-testid="artifacts-tab-off">
          <Package size={16} aria-hidden="true" />
          <p>
            Artifacts are off for this session. Turn the tool on and the agent
            can publish files and static sites at a stable link.
          </p>
          <div className={styles.btnRow}>
            <button
              type="button"
              className={styles.primary}
              onClick={(e) => setDialog(e.currentTarget)}
            >
              <Power size={14} aria-hidden="true" />
              Turn on…
            </button>
          </div>
        </div>
      ) : (
        <div className={styles.tabHead}>
          <div className={styles.toolRow}>
            <span className={styles.tool} data-testid="artifacts-tool-mode">
              <Package size={14} aria-hidden="true" />
              Tool · <b>{mode ? modeLabel(mode) : "…"}</b>
            </span>
            <button
              type="button"
              className={styles.rowBtn}
              onClick={(e) => setDialog(e.currentTarget)}
            >
              <SlidersHorizontal size={14} aria-hidden="true" />
              Change
            </button>
          </div>
          {items !== null && (
            <span className={styles.meta}>
              <b>{items.length}</b> published
              <span className={styles.dot}>
                <b>{pub}</b> public
              </span>
              {inMission && (
                <span className={styles.dot}>also on the mission shelf</span>
              )}
            </span>
          )}
        </div>
      )}
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : items === null ? (
        <Loader
          kind="skeleton"
          label="Reading this session's artifacts"
          rows={2}
        />
      ) : items.length > 0 ? (
        <div className={styles.tabGrid}>
          {items.map((a) => (
            <Tile key={a.id} a={a} />
          ))}
        </div>
      ) : mode && mode !== "off" ? (
        <p className={styles.note}>
          Nothing published yet. Ask the agent to publish a file or a static
          site.
        </p>
      ) : null}
      {foot}
      {dialog && (
        <SessionActivationDialog
          session={{ id: sessionKey, engine, cwd }}
          returnFocusTo={dialog}
          onClose={() => {
            setDialog(null);
            load();
          }}
        />
      )}
    </div>
  );
}
