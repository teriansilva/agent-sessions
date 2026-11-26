/** Settings → AI → HQ → Authority (#2390, epic #2070 §1): how much HQ does on its own. Under Ask,
 *  HQ stages a card and nothing happens until the Commander confirms it; under Act, HQ runs it at
 *  once and the conversation shows it as done by HQ. The server owns the classes, which of them
 *  can act in this version, and the revision every write is compared against (409 when stale).
 *  HQ itself can never change this page's setting (the route refuses HQ's in-process calls). */
import { useCallback, useEffect, useRef, useState } from "react";
import { Stamp } from "../components/hud/Meters";
import { ApiError, api } from "../lib/api";
import type {
  HqAct,
  HqAuthority,
  HqAuthorityClass,
  HqAuthorityPatch,
  HqAuthorityPreset,
  HqAuthorityValue,
} from "../types/hq";
import styles from "./Settings.module.css";

const PRESETS: { id: Exclude<HqAuthorityPreset, "custom">; label: string; detail: string }[] = [
  { id: "ask_first", label: "Ask first", detail: "Every change is a card. Nothing happens until you confirm it." },
  { id: "standard", label: "Standard", detail: "HQ makes reversible changes itself. Everything else asks." },
  { id: "full", label: "Full control", detail: "Every class acts, the RISKY ones included." },
];

const CLASSES: { id: HqAuthorityClass; label: string; examples: string }[] = [
  {
    id: "reversible",
    label: "Reversible changes",
    examples:
      "Rename, tag, colour, favourite, move to a project, unarchive, a default project, Library saves kept switched off.",
  },
  {
    id: "starts",
    label: "Starts or sends",
    examples:
      "Start an API session, run an automation now, send a connector message, relay to a mission.",
  },
  {
    id: "destructive",
    label: "Destructive",
    examples:
      "Archive or close a session (stops its agent), archive older sessions, archive or delete a project, save over a playbook, forget a memory note.",
  },
  {
    id: "external",
    label: "External",
    examples: "Calls to your AI endpoint, git push, public shares, update now.",
  },
  {
    id: "widens",
    label: "Widens access",
    examples: "Permission bypass, autonomy up, automations on, secret agent access, MCP servers on.",
  },
];

const LOCKED = [
  "This Authority setting",
  "HQ's guarded conversation prompt",
  "Your login: password, two-factor, sign-out, device links",
];
const YOURS =
  "Approving a mission plan, its consent, holds and budget; credentials and endpoints; who HQ " +
  "may message; plugin installs; pairing; analytics and memory consent.";

const OUTCOME: Record<string, string> = {
  done: "Done",
  partly_done: "Partly done",
  failed: "Failed",
  stale: "Out of date",
  interrupted: "Interrupted",
  unconfirmed: "Ran, unconfirmed",
  started: "Started",
  sent: "Sent",
  deciding: "Running",
};

const when = (ts: number) =>
  new Date(ts * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

/** A response this page can draw; anything else reads as unreadable (it never crashes the page). */
function isAuthority(v: unknown): v is HqAuthority {
  if (!v || typeof v !== "object") return false;
  const a = v as HqAuthority;
  return (
    !!a.classes &&
    typeof a.classes === "object" &&
    typeof a.preset === "string" &&
    typeof a.revision === "number" &&
    Array.isArray(a.actable) &&
    Array.isArray(a.risky)
  );
}

/** What an "also after untrusted text" switch lets a hostile report do, per class (#2391, #2396). */
const UNTRUSTED_ALLOWS: Partial<Record<HqAuthorityClass, string>> = {
  starts: "start or send",
  destructive: "archive or delete",
};

/** D4 (#2391): "also after HQ read untrusted text" for one class. Off by default; on is RISKY and
 *  says what it allows. The whole row is the 44 px target; the switch is a real `role="switch"`. */
function UntrustedSwitch({
  id,
  label,
  on,
  disabled,
  onChange,
}: {
  id: HqAuthorityClass;
  label: string;
  on: boolean;
  disabled: boolean;
  onChange: (on: boolean) => void;
}) {
  const hint = `hq-authority-untrusted-${id}-hint`;
  return (
    <label
      className={`${styles.authorityUntrusted} ${on ? styles.authorityUntrustedOn : ""}`}
      data-testid={`hq-authority-untrusted-${id}`}
    >
      <span className={styles.authorityUntrustedText} id={hint}>
        Also after HQ read text from a session, connector or file in this conversation
        {on ? (
          <>
            : <b>on</b>. A hostile report can now get HQ to {UNTRUSTED_ALLOWS[id] ?? "act"}.
          </>
        ) : (
          <>. Off: HQ asks first then.</>
        )}
      </span>
      <Stamp tone="down">{on ? "RISKY · ON" : "RISKY"}</Stamp>
      <input
        type="checkbox"
        role="switch"
        className={styles.authoritySwitch}
        checked={on}
        disabled={disabled}
        aria-describedby={hint}
        aria-label={`${label}: also after untrusted text`}
        onChange={(e) => onChange(e.currentTarget.checked)}
      />
    </label>
  );
}

export function HqAuthoritySettings() {
  const live = useRef(true);
  const [state, setState] = useState<HqAuthority | null>(null);
  const [readError, setReadError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [saved, setSaved] = useState(false);
  const [acts, setActs] = useState<HqAct[] | null>(null);
  const [actsError, setActsError] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await api.hqAuthority();
      if (!isAuthority(next)) throw new Error("unreadable");
      if (live.current) {
        setState(next);
        setReadError(false);
        setError(null);
        setStale(false);
      }
    } catch {
      if (live.current) setReadError(true);
    }
    try {
      const r = await api.hqActs();
      if (live.current) {
        setActs(Array.isArray(r?.acts) ? r.acts.filter((a) => a && typeof a === "object") : []);
        setActsError(false);
      }
    } catch {
      if (live.current) setActsError(true);
    }
  }, []);

  useEffect(() => {
    live.current = true;
    // The first read of a server setting: every setState in it lands after an await.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
    return () => {
      live.current = false;
    };
  }, [load]);

  const save = async (patch: HqAuthorityPatch) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const next = await api.setHqAuthority(patch);
      if (!isAuthority(next)) throw new Error("The saved setting could not be read. Reload settings.");
      if (live.current) {
        setState(next);
        setSaved(true);
      }
    } catch (e) {
      if (!live.current) return;
      if (e instanceof ApiError && e.status === 409) {
        setStale(true);
        setError("Authority was changed elsewhere. Reload to see the current setting.");
      } else setError(e instanceof Error ? e.message : "That didn’t save. Try again.");
    } finally {
      if (live.current) setBusy(false);
    }
  };

  const setPreset = (id: Exclude<HqAuthorityPreset, "custom">) =>
    state && state.preset !== id && void save({ revision: state.revision, preset: id });
  const setClass = (id: HqAuthorityClass, value: HqAuthorityValue) =>
    state &&
    state.classes[id] !== value &&
    void save({ revision: state.revision, classes: { [id]: value } });
  const setUntrusted = (id: HqAuthorityClass, on: boolean) =>
    state && void save({ revision: state.revision, untrusted: { [id]: on } });
  const off = busy || stale || !state;

  return (
    <section className={styles.section} aria-labelledby="hq-authority-h" data-testid="hq-authority">
      <h2 id="hq-authority-h">Authority</h2>
      <p className={styles.hint}>
        How much HQ does on its own. <b>Ask</b>: HQ puts a card in the conversation and nothing
        happens until you confirm it. <b>Act</b>: HQ does it at once, through the same button you
        would press, and the conversation shows what it did.
      </p>
      {readError && (
        <div role="alert" className={styles.err}>
          <p>Authority could not be read. HQ asks first until it can be.</p>
          <button type="button" className={styles.secBtnGhost} onClick={() => void load()}>
            Reload
          </button>
        </div>
      )}
      {state && (
        <>
          <div className={styles.aiField}>
            <span className={styles.aiFieldLabel} id="hq-authority-preset-h">
              Preset{state.preset === "custom" ? " · Custom" : ""}
            </span>
            <div
              className={styles.authorityPresets}
              role="radiogroup"
              aria-labelledby="hq-authority-preset-h"
            >
              {PRESETS.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  role="radio"
                  aria-checked={state.preset === p.id}
                  className={styles.authorityPreset}
                  disabled={off}
                  onClick={() => setPreset(p.id)}
                  data-testid={`hq-authority-preset-${p.id}`}
                >
                  <span className={styles.authorityPresetName}>{p.label}</span>
                  <span className={styles.authorityPresetDetail}>{p.detail}</span>
                </button>
              ))}
            </div>
            {state.preset === "custom" && (
              <p className={styles.hint} data-testid="hq-authority-custom">
                Your own mix of the rows below.
              </p>
            )}
          </div>

          <ul className={styles.authorityRows} aria-label="Authority by class">
            <li className={styles.authorityRow} data-testid="hq-authority-row-screen">
              <div className={styles.authorityMain}>
                <span className={styles.authorityName}>Screen</span>
                <span className={styles.authorityExamples}>
                  Open, arrange and navigate what your screen shows. Changes nothing else.
                </span>
              </div>
              <span className={styles.authorityFixed}>Always acts</span>
            </li>
            {CLASSES.map((c) => {
              const value = state.classes[c.id] ?? "ask";
              const risky = state.risky.includes(c.id);
              const later = !state.actable.includes(c.id);
              const nameId = `hq-authority-${c.id}`;
              return (
                <li
                  key={c.id}
                  className={`${styles.authorityRow} ${risky ? styles.authorityRisky : ""}`}
                  data-testid={`hq-authority-row-${c.id}`}
                >
                  <div className={styles.authorityMain}>
                    <span className={styles.authorityName} id={nameId}>
                      {c.label}
                      {risky ? <Stamp tone="down">RISKY</Stamp> : null}
                    </span>
                    <span className={styles.authorityExamples}>{c.examples}</span>
                    {later && (
                      <span className={styles.authorityLater} data-testid={`hq-authority-later-${c.id}`}>
                        {value === "act"
                          ? "Kept · HQ still asks for these until a later update lets it act."
                          : "HQ can't act on these yet; a later update adds it."}
                      </span>
                    )}
                    {state.untrusted_classes?.includes(c.id) && (
                      <UntrustedSwitch
                        id={c.id}
                        label={c.label}
                        on={state.untrusted?.[c.id] === true}
                        disabled={off}
                        onChange={(on) => setUntrusted(c.id, on)}
                      />
                    )}
                  </div>
                  <div className={styles.aiSeg} role="radiogroup" aria-labelledby={nameId}>
                    {(["ask", "act"] as const).map((v) => (
                      <button
                        key={v}
                        type="button"
                        role="radio"
                        aria-checked={value === v}
                        disabled={off}
                        onClick={() => setClass(c.id, v)}
                      >
                        {v === "ask" ? "Ask" : "Act"}
                      </button>
                    ))}
                  </div>
                </li>
              );
            })}
          </ul>
          <p className={styles.hint}>
            Screen and reversible changes act even after HQ has read text an agent or a website
            wrote (a session&rsquo;s screen, a report, a file). Starts and sends ask first after
            that, unless you switch them on above. HQ starts API sessions itself, never with
            permission bypass; a terminal session is always started on its card. Messages HQ
            types into sessions follow Autonomy below.
          </p>
        </>
      )}
      {error && (
        <div role="alert" className={styles.err}>
          <p>{error}</p>
          {stale && (
            <button type="button" className={styles.secBtnGhost} onClick={() => void load()}>
              Reload
            </button>
          )}
        </div>
      )}
      {saved && (
        <p role="status" className={styles.ok}>
          Saved. It applies from your next question; lowering it also applies to a question HQ is working on now.
        </p>
      )}

      <div className={styles.authorityLocked} data-testid="hq-authority-locked">
        <span className={styles.authorityLockLabel}>Locked</span>
        <p>HQ never changes these, in either direction, at any level:</p>
        <ul>
          {LOCKED.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
        <p className={styles.hint}>Always yours, never HQ&rsquo;s: {YOURS}</p>
      </div>

      <h3 className={styles.subhead} id="hq-acts-h">
        Recent acts
      </h3>
      {actsError ? (
        <p className={styles.hint} role="status">
          The acts log could not be read just now.
        </p>
      ) : acts && acts.length === 0 ? (
        <p className={styles.hint} data-testid="hq-acts-empty">
          HQ hasn&rsquo;t acted on its own yet. Every act shows here and in its conversation.
        </p>
      ) : acts ? (
        <ul className={styles.activityList} aria-labelledby="hq-acts-h" data-testid="hq-acts">
          {acts.slice(0, 20).map((a, n) => (
            <li key={`${a.ts}-${n}`} className={styles.authorityAct}>
              <span className={styles.authorityActName}>
                {a.label}
                {a.title ? ` · ${a.title}` : ""}
              </span>
              <span className={styles.authorityActMeta}>
                {OUTCOME[a.state] ?? a.state} · {a.via === "call" ? "on a call" : "typed"} ·{" "}
                {when(a.ts)}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
