/** The call's Audio sheet (#1670): the microphones and speakers this browser reports, switched
 *  mid-call. A choice is remembered on this device. Where the browser can't pick a speaker (phones,
 *  Safari, Firefox), the sheet says how to switch it instead. A refused switch keeps what is in use
 *  and says why; a device unplugged mid-call falls back to the default and says so. */
import { useCallback, useEffect, useId, useState } from "react";

import { remember, type DeviceKind, type DeviceLists, type DeviceOption } from "./audioDevices";
import { SUPERSEDED, type DeviceAudio } from "./callAudio";
import s from "./call.module.css";

export function AudioSheet({ api, id }: { api: DeviceAudio; id: string }) {
  const uid = useId();
  const [lists, setLists] = useState<DeviceLists | null>(null);
  const [cur, setCur] = useState(() => api.current());
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<{ kind: DeviceKind; message: string } | null>(null);

  const refresh = useCallback(
    () =>
      api
        .devices()
        .catch((): DeviceLists => ({ inputs: [], outputs: [] }))
        .then((l) => {
          setLists(l);
          setCur(api.current());
        }),
    [api],
  );

  useEffect(() => {
    void refresh();
    const md = navigator.mediaDevices;
    const changed = () => void refresh();
    md?.addEventListener?.("devicechange", changed);
    api.onNotice((message) => {
      setNotice(message);
      void refresh();
    });
    return () => {
      md?.removeEventListener?.("devicechange", changed);
      api.onNotice(null);
    };
  }, [api, refresh]);

  const pick = async (kind: DeviceKind, opt: DeviceOption) => {
    setError(null);
    const err = kind === "input" ? await api.setInput(opt.id) : await api.setOutput(opt.id);
    if (err === SUPERSEDED) {
      // A newer choice overtook this one: nothing changed, nothing is remembered (#1670 review).
    } else if (err) setError({ kind, message: err });
    else {
      remember(kind, opt);
      setNotice(null);
    }
    setCur(api.current());
  };

  const inUse = (kind: DeviceKind, opt: DeviceOption) => {
    const now = kind === "input" ? cur.input : cur.output;
    return now === opt.id || (now === "" && opt.id === "default");
  };

  /** The browser's own speaker picker (#1670): called synchronously from this tap, which is the
   *  gesture it needs — nothing is awaited before it. */
  const pickSpeaker = () => {
    setError(null);
    const opened = api.pickOutput();
    void opened.then(({ result, device }) => {
      if (result === SUPERSEDED) return; // cancelled, or overtaken: nothing changed
      if (result) setError({ kind: "output", message: result });
      else if (device) {
        remember("output", device);
        setNotice(null);
      }
      void refresh();
    });
  };

  const group = (kind: DeviceKind, legend: string, options: DeviceOption[]) => (
    <fieldset className={s.devGroup} data-testid={`call-audio-${kind}`}>
      <legend>{legend}</legend>
      {error?.kind === kind ? (
        <p className={s.devErr} role="alert" data-testid={`call-audio-${kind}-error`}>
          {error.message}
        </p>
      ) : null}
      {options.map((opt) => (
        <label key={opt.id} className={s.devRow}>
          <input
            type="radio"
            name={`${uid}-${kind}`}
            checked={inUse(kind, opt)}
            onChange={() => void pick(kind, opt)}
          />
          <span className={s.devName}>{opt.label}</span>
        </label>
      ))}
      {kind === "output" && api.canPickOutput() ? (
        <button
          type="button"
          className={s.devRow}
          onClick={pickSpeaker}
          data-testid="call-audio-output-pick"
        >
          <span className={s.devName}>Another speaker…</span>
        </button>
      ) : null}
    </fieldset>
  );

  return (
    <section className={s.audioSheet} id={id} aria-label="Audio" data-testid="call-audio-sheet">
      <p className={s.audioHead}>Audio · this device</p>
      {notice ? (
        <p className={s.devWarn} role="status" data-testid="call-audio-notice">
          {notice}
        </p>
      ) : null}
      {lists ? group("input", "Microphone", lists.inputs) : null}
      {lists && api.canSelectOutput() ? (
        group("output", "Speaker", lists.outputs)
      ) : lists ? (
        <div className={s.devGroup} data-testid="call-audio-output-note">
          <p className={s.devNote}>
            <b>Your device picks the speaker</b> — this browser can’t switch it. A connected
            headset is used automatically; on iPhone, Control Center → audio output.
          </p>
        </div>
      ) : null}
    </section>
  );
}
