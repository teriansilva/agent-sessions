/** The setup wizard's Connect HQ step (#1880): every connector the catalog offers, as a tile, with
 *  Settings' own card opened inline to set one up.
 *
 *  **Nothing here is a second implementation.** The tiles read `/api/connectors` exactly as
 *  Settings → AI → HQ connectors does, a tile's state is `statusLine` of its instance (the same
 *  words and LED tone the Settings card shows), and Set up / Manage / Fix it open the very
 *  `ConnectorCard` Settings renders. Every save, test and switch goes through that card's routes,
 *  so the wizard adds no endpoint and no authority. */
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../../lib/api";
import {
  statusLine,
  tileInstance,
  tileLine,
  type ConnectorCatalog,
  type ConnectorInstance,
  type ConnectorManifest,
} from "../../lib/connectors";
import { ConnectorCard } from "../../routes/ConnectorsSettings";
import settings from "../../routes/Settings.module.css";
import styles from "./ConnectHqStep.module.css";

function message(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

export function ConnectHqStep({ hintClass }: { hintClass?: string }) {
  const [data, setData] = useState<ConnectorCatalog | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [addError, setAddError] = useState<string | null>(null);
  /** The card open in place of its tile: which manifest's tile, showing which instance. */
  const [open, setOpen] = useState<{ manifest: string; inst: string } | null>(null);
  /** The instance just added, as the add answered it: its card opens (and its first field takes
   *  focus) before the re-read that lists it has landed. Dropped once a read lists it. */
  const [added, setAdded] = useState<ConnectorInstance | null>(null);

  // Only the newest read installs its answer (the Settings page's rule, Hermes on #1828).
  const latestRead = useRef(0);
  const reload = useCallback(() => {
    const seq = ++latestRead.current;
    return api
      .connectors()
      .then((r) => {
        if (seq !== latestRead.current) return;
        setData(r);
        setLoadError(null);
      })
      .catch((e: unknown) => {
        if (seq === latestRead.current)
          setLoadError(message(e, "Couldn’t load the connectors."));
      });
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);

  const instances = data?.instances ?? [];
  if (added && instances.some((i) => i.id === added.id)) setAdded(null);
  const openInst = open
    ? (instances.find((i) => i.id === open.inst) ??
      (added?.id === open.inst ? added : undefined))
    : undefined;
  // The open card's instance is gone (its own Remove, or removed elsewhere): fold back to the tile.
  if (open && data && !openInst) setOpen(null);

  const setUp = async (m: ConnectorManifest) => {
    setAddError(null);
    const existing = tileInstance(m, instances);
    if (existing) {
      setOpen({ manifest: m.id, inst: existing.id });
      return;
    }
    setAdding(m.id);
    try {
      const inst = await api.addConnector(m.id);
      setAdded(inst);
      setOpen({ manifest: m.id, inst: inst.id });
    } catch (e) {
      setAddError(message(e, `Couldn’t add ${m.label}.`));
    } finally {
      setAdding(null);
      await reload();
    }
  };

  return (
    <div className={styles.step} data-testid="connect-hq-step">
      {loadError && (
        <p className={settings.err} role="alert">
          {loadError}
        </p>
      )}
      {addError && (
        <p className={settings.err} role="alert">
          {addError}
        </p>
      )}
      <ul className={styles.grid} aria-label="Connectors">
        {(data?.catalog ?? []).map((m) => {
          if (open?.manifest === m.id && openInst)
            return (
              <ConnectorCard
                key={m.id}
                inst={openInst}
                manifest={m}
                disclosure={data?.disclosure ?? ""}
                focusOnMount={added?.id === openInst.id}
                onChanged={reload}
                onClose={() => setOpen(null)}
              />
            );
          const inst = tileInstance(m, instances);
          // #1746 allows several accounts per connector; the tile shows one and says so, so
          // another account is never hidden, and Set up never adds a duplicate.
          const count = instances.filter((i) => i.manifest_id === m.id).length;
          const shown = inst ? statusLine(inst, m.category) : null;
          const verb = !shown ? "Set up" : shown.tone === "down" ? "Fix" : "Manage";
          return (
            <li
              key={m.id}
              className={styles.tile}
              data-testid="connect-hq-tile"
              data-connector={m.id}
            >
              <div className={styles.tileHead}>
                <b className={styles.name}>{m.label}</b>
                <span className={styles.state} data-testid="connect-hq-state">
                  {shown && (
                    <span className={`hud-led ${shown.tone}`} aria-hidden="true" />
                  )}
                  {shown ? shown.text : "Not set up"}
                </span>
              </div>
              <p className={styles.line}>{tileLine(m)}</p>
              <span className={styles.meta}>
                by {m.publisher === "battlelab" ? "BattleLab" : m.publisher} ·{" "}
                {m.capabilities.read.length ? "read-only" : "sends only what you tap"}
              </span>
              {count > 1 && (
                <span className={styles.meta} data-testid="connect-hq-more">
                  {count} set up · this shows one; all of them are in Settings → AI → HQ connectors
                </span>
              )}
              <button
                type="button"
                className={`${settings.secBtnGhost} ${styles.tileBtn}`}
                disabled={adding !== null}
                onClick={() => void setUp(m)}
              >
                {adding === m.id ? "Adding…" : `${verb} ${m.label}`}
              </button>
            </li>
          );
        })}
      </ul>
      <p className={hintClass}>
        Not now? They wait under <b>Settings → AI → HQ connectors</b>, where every connector
        can be set up, tested or removed later.
      </p>
    </div>
  );
}
