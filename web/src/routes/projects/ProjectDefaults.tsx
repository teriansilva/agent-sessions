/** Project → Session defaults (#2143): what a NEW session in this project starts with (#1508) and
 *  the project's artifacts default (#1932 P2), drafted together and written by ONE save bar.
 *
 *  The bar spans two stores and two writes (`PATCH /api/projects/{pid} {launch_defaults}` and
 *  `PUT /api/artifacts/activation`) that are not one transaction, so it never claims more than the
 *  server confirmed: on a partial success the written half becomes the saved state, the failed
 *  half stays unsaved with its error in place, and the bar keeps offering Save for what is left. */
import { useCallback, useEffect, useState } from "react";
import { ActivationControl } from "../../components/artifacts/ActivationControl";
import { Callsign, Panel } from "../../components/hud/Panel";
import { Led } from "../../components/hud/Meters";
import { ProjectSessionDefaults } from "../../components/projects/ProjectSessionDefaults";
import action from "../../components/ui/actionButton.module.css";
import { api, ApiError } from "../../lib/api";
import { cleanLaunchDefaults } from "../../lib/launchDefaults";
import type { ArtifactMode } from "../../types/artifacts";
import type { LaunchDefaults, ProjectEntity } from "../../types/api";
import { ReadFailed } from "./ProjectBits";
import { readError } from "./projectsData";
import styles from "./projects.module.css";

const same = (a: LaunchDefaults, b: LaunchDefaults) =>
  JSON.stringify(cleanLaunchDefaults(a)) === JSON.stringify(cleanLaunchDefaults(b));

function errMessage(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

export function ProjectDefaults({ entity, onSaved }: { entity: ProjectEntity; onSaved: () => Promise<unknown> }) {
  // What the server holds (`saved*`) and what the operator is editing (`draft*`), per store.
  const [savedLaunch, setSavedLaunch] = useState<LaunchDefaults>(entity.launch_defaults ?? {});
  const [draft, setDraft] = useState<LaunchDefaults>(entity.launch_defaults ?? {});
  // The artifacts default lives in its own store and is read here; unreadable is said in place
  // (never a guessed "off").
  const [artifacts, setArtifacts] = useState<{ saved: ArtifactMode | null; draft: ArtifactMode | null } | null>(null);
  const [artifactsError, setArtifactsError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [launchError, setLaunchError] = useState<string | null>(null);
  const [saveArtifactsError, setSaveArtifactsError] = useState<string | null>(null);
  const [status, setStatus] = useState("");

  const readArtifacts = useCallback(() => {
    let live = true;
    api
      .artifactActivation("project", entity.id)
      .then((a) => live && setArtifacts({ saved: a.mode, draft: a.mode }))
      .catch((e) => live && setArtifactsError(readError(e, "The artifacts store didn’t answer.")));
    return () => {
      live = false;
    };
  }, [entity.id]);
  useEffect(() => readArtifacts(), [readArtifacts]);

  const launchDirty = !same(draft, savedLaunch);
  const artifactsDirty = !!artifacts && artifacts.draft !== artifacts.saved;
  const dirty = launchDirty || artifactsDirty;

  const save = async () => {
    setBusy(true);
    setStatus("");
    setLaunchError(null);
    setSaveArtifactsError(null);
    let launchOk = !launchDirty;
    if (launchDirty) {
      try {
        await api.patchProject(entity.id, { launch_defaults: draft });
        setSavedLaunch(draft);
        launchOk = true;
      } catch (e) {
        setLaunchError(errMessage(e, "Couldn’t save the session defaults."));
      }
    }
    let artifactsOk = !artifactsDirty;
    if (artifactsDirty && artifacts) {
      try {
        await api.setArtifactActivation("project", entity.id, artifacts.draft);
        setArtifacts({ ...artifacts, saved: artifacts.draft });
        artifactsOk = true;
      } catch (e) {
        const msg = errMessage(e, "Couldn’t save the artifacts default.");
        // A partial success says which half landed: never "saved" for the half that did not.
        setSaveArtifactsError(
          launchDirty && launchOk ? `Session defaults saved; the artifacts default didn’t: ${msg}` : msg,
        );
      }
    }
    if (launchDirty && launchOk) await onSaved().catch(() => {});
    setBusy(false);
    if (launchOk && artifactsOk) setStatus("Saved.");
  };
  const cancel = () => {
    setDraft(savedLaunch);
    if (artifacts) setArtifacts({ ...artifacts, draft: artifacts.saved });
    setLaunchError(null);
    setSaveArtifactsError(null);
    setStatus("");
  };
  const what = [launchDirty && "session defaults", artifactsDirty && "artifacts default"].filter(Boolean).join(" · ");

  return (
    <div className={styles.body}>
      <Panel tone="quiet" className={styles.panel} aria-labelledby="defaults-h">
        <Callsign as="h2" id="defaults-h" title="Agent for new sessions" />
        <section aria-label={`Session defaults for ${entity.name}`}>
          <ProjectSessionDefaults
            value={draft}
            onChange={(next) => {
              setDraft(next);
              setStatus("");
            }}
            disabled={busy}
          />
        </section>
        {launchError && (
          <p className="nj-error" role="alert" data-testid="defaults-save-error">
            {launchError}
          </p>
        )}
      </Panel>
      <Panel tone="quiet" className={styles.panel} aria-labelledby="artifacts-h">
        <Callsign as="h2" id="artifacts-h" title="Artifacts tool" />
        {artifacts ? (
          <ActivationControl
            value={artifacts.draft}
            onChange={(m) => {
              setArtifacts({ ...artifacts, draft: m });
              setStatus("");
            }}
            label="Default mode"
            note="for new sessions"
            busy={busy}
            source="Order: a session's own setting wins over its mission's, and a mission's wins over this default. Running sessions change at their next restart."
            clearLabel="Clear: no project default"
            testId="project-artifacts-default"
          />
        ) : artifactsError ? (
          <ReadFailed
            title="Couldn’t read the artifacts default"
            detail={`${artifactsError}. The session defaults above still save.`}
            onRetry={() => {
              setArtifactsError(null);
              readArtifacts();
            }}
            testId="artifacts-read-failed"
          />
        ) : (
          <p className={styles.quiet}>Reading the artifacts default…</p>
        )}
        {saveArtifactsError && (
          <p className="nj-error" role="alert" data-testid="artifacts-save-error">
            {saveArtifactsError}
          </p>
        )}
      </Panel>
      {dirty ? (
        <div className={`nj-frame-sm ${styles.savebar}`} data-testid="defaults-savebar">
          <span className={styles.led}>
            <Led tone="draft" /> Unsaved changes
          </span>
          <span className={styles.what}>{what}</span>
          <button type="button" className={`${action.ghost} ${action.compact}`} disabled={busy} onClick={cancel}>
            Cancel
          </button>
          <button
            type="button"
            className={`${action.primary} ${action.compact}`}
            disabled={busy}
            onClick={() => void save()}
          >
            Save session defaults
          </button>
        </div>
      ) : (
        status && (
          <p className={styles.quiet} role="status">
            {status}
          </p>
        )
      )}
    </div>
  );
}
