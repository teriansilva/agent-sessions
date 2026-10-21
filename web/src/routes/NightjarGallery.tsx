/** The NIGHTJAR primitives gallery (#1531, docs/design.md v2 §6): every primitive in its states,
 *  in whichever mode, motion mode and viewport the page is opened in. It is the design system's
 *  Playwright baseline (`e2e/nightjar-gallery.spec.ts`), not a product surface: unlinked, behind
 *  the normal login. */
import { useEffect, useRef, useState } from "react";

import { Chip, Disclosure, Field, Seg, Switch, Tabs } from "../components/hud/Controls";
import { Loader } from "../components/hud/Loader";
import { Gauge, Led, Pips, Readout, Stamp } from "../components/hud/Meters";
import { Callsign, Panel } from "../components/hud/Panel";
import { StreamText } from "../components/hud/StreamText";
import btn from "../components/ui/actionButton.module.css";
import type { MotionChoice } from "../theme/motion";
import { useMotion } from "../theme/motionStore";
import g from "./NightjarGallery.module.css";

const STREAM =
  "Two things need you, Commander. The API keys review is stopped on your call: rotate the old key now, or after the release? The share-mask timing bound is green again.";

const MOTION_OPTIONS: { value: MotionChoice; label: string }[] = [
  { value: "system", label: "Device" },
  { value: "full", label: "Full" },
  { value: "reduced", label: "Reduced" },
  { value: "off", label: "Off" },
];

/** A fake model stream: a word or two every 90 ms, the way tokens arrive. */
function useStreamDemo() {
  const [n, setN] = useState(0);
  const [run, setRun] = useState(0);
  useEffect(() => {
    if (!run) return;
    const id = setInterval(() => {
      setN((x) => {
        const next = STREAM.indexOf(" ", x + 4);
        return next < 0 ? STREAM.length : next;
      });
    }, 90);
    return () => clearInterval(id);
  }, [run]);
  const start = () => {
    setN(0);
    setRun((r) => r + 1);
  };
  return { text: STREAM.slice(0, n), started: run > 0, start };
}

export default function NightjarGallery() {
  const motion = useMotion();
  const [seg, setSeg] = useState<"all" | "live" | "idle">("all");
  const [tab, setTab] = useState<"brief" | "plan" | "log">("brief");
  const [on, setOn] = useState(true);
  const stream = useStreamDemo();
  const [rows, setRows] = useState<string[]>(["ALPHA-1 // working", "BRAVO-2 // needs you"]);
  const rowId = useRef(3);
  const [readout, setReadout] = useState(7);
  // Remounting the power-on panel replays its one-shot choreography (the sweep captures it mid-way).
  const [powerKey, setPowerKey] = useState(0);

  return (
    <main className={g.page} data-testid="nightjar-gallery">
      <div className={g.inner}>
        <div className={g.row}>
          <h1 className="nj-h1">Primitives</h1>
          <Seg
            label="Motion (gallery)"
            options={MOTION_OPTIONS}
            value={motion.choice}
            onChange={motion.setMotion}
            testId="gallery-motion"
          />
          <span className={g.word} data-testid="gallery-mode">
            mode // {motion.mode}
            {motion.paused ? ` // paused (${motion.paused})` : ""}
          </span>
        </div>

        <div className={g.grid}>
          <Panel data-testid="g-panel-default">
            <div className={g.body}>
              <Callsign title="Panel" value="default" meta="rail full" />
              <p className={g.prose}>An opaque face on a chamfered edge, a lit rail, one tick.</p>
            </div>
          </Panel>
          <Panel tone="hero" glow liveRail data-testid="g-panel-hero">
            <div className={g.body}>
              <Callsign title="Panel" value="hero" meta="glow · live rail" />
              <p className={g.prose}>One per view: the "you are here" panel.</p>
            </div>
          </Panel>
          <Panel tone="quiet" rail="short" data-testid="g-panel-quiet">
            <div className={g.body}>
              <Callsign title="Panel" value="quiet" meta="settings" />
              <p className={g.prose}>Settings and admin surfaces: no rail glow.</p>
            </div>
          </Panel>
          <Panel tone="alert" data-testid="g-panel-alert">
            <div className={g.body}>
              <Callsign title="Panel" value="alert" meta="active failure only" />
              <p className={g.prose}>Red is reserved for a failure in progress.</p>
            </div>
          </Panel>
        </div>

        <div className={g.grid}>
          <Panel>
            <div className={g.body}>
              <Callsign title="LEDs" value="with words" />
              <div className={g.row}>
                {(["up", "att", "down", "idle", "draft", "accent"] as const).map((t) => (
                  <span key={t} className={g.word}>
                    <Led tone={t} />
                    {t === "att" ? "needs you" : t}
                  </span>
                ))}
              </div>
              <Callsign title="Stamps" />
              <div className={g.row}>
                <Stamp tone="ok">Secured</Stamp>
                <Stamp tone="warn">Holding</Stamp>
                <Stamp tone="down">Failed</Stamp>
                <Stamp tone="hud">Briefed</Stamp>
              </div>
            </div>
          </Panel>
          <Panel>
            <div className={g.body}>
              <Callsign title="Meters" value="pips · gauge · readout" />
              <div className={g.row}>
                <Pips value={4} label="Readiness 4 of 5" />
                <Pips value={2} tone="warn" label="Reserves 2 of 5" />
                <Pips value={1} tone="down" label="Health 1 of 5" />
              </div>
              <Gauge value={0.62} label="Plan quota" ticks={["0", "50", "100"]} />
              <Gauge value={0.09} label="Reserves low" ticks={["0", "50", "100"]} />
              <div className={g.row}>
                <Readout value={String(readout).padStart(2, "0")} unit="units" testId="g-readout" />
                <button type="button" className={btn.ghost} onClick={() => setReadout((x) => x + 1)}>
                  Change value
                </button>
              </div>
            </div>
          </Panel>
          <Panel>
            <div className={g.body}>
              <Callsign title="Chips" value="filters" />
              <div className={g.row}>
                <Chip on count={24} onClick={() => {}}>
                  All
                </Chip>
                <Chip count={3} onClick={() => {}}>
                  Live
                </Chip>
                <Chip>Static</Chip>
              </div>
              <Callsign title="Segmented" />
              <Seg
                label="Scope"
                options={[
                  { value: "all", label: "All" },
                  { value: "live", label: "Live" },
                  { value: "idle", label: "Idle" },
                ]}
                value={seg}
                onChange={setSeg}
              />
            </div>
          </Panel>
        </div>

        <div className={g.grid}>
          <Panel>
            <div className={g.body}>
              <Callsign title="Buttons" />
              <div className={g.row}>
                <button type="button" className={btn.primary}>
                  Deploy ▸
                </button>
                <button type="button" className={btn.ghost}>
                  Cancel
                </button>
                <button type="button" className={btn.icon} aria-label="More">
                  ⋯
                </button>
              </div>
              <div className={g.row}>
                <button type="button" className={btn.danger}>
                  Archive
                </button>
                <button type="button" className={btn.dangerFilled}>
                  Delete project
                </button>
                <button type="button" className={btn.primary} disabled>
                  Disabled
                </button>
              </div>
            </div>
          </Panel>
          <Panel>
            <div className={g.body}>
              <Callsign title="Fields" />
              <Field label="Callsign" defaultValue="viper-8231" hint="Lowercase, a dash, four digits." mono />
              <Field label="Unit" as="select" defaultValue="alpha">
                <option value="alpha">Unit Alpha</option>
                <option value="bravo">Unit Bravo</option>
              </Field>
              <Field label="Orders" as="textarea" rows={2} defaultValue="Rotate after the release." />
              <Field label="Project name" defaultValue="atlas" error="That name is taken." />
              <div className={g.row}>
                <label className={g.word}>
                  <input type="checkbox" className="nj-check" defaultChecked /> Notify
                </label>
                <label className={g.word}>
                  <input type="radio" name="g-r" className="nj-check" defaultChecked /> Hold
                </label>
                <span className={g.word}>
                  <Switch checked={on} onChange={setOn} label="Auto-sort" /> Auto-sort
                </span>
              </div>
            </div>
          </Panel>
          <Panel>
            <div className={g.body}>
              <Tabs
                label="Mission detail"
                tabs={[
                  { id: "brief", label: "Brief" },
                  { id: "plan", label: "Plan" },
                  { id: "log", label: "Log" },
                ]}
                value={tab}
                onChange={setTab}
                panel={(id) => <p className={g.prose}>The {id} tab's panel cross-fades in.</p>}
              />
              <Disclosure summary="Evidence" testId="g-disclosure">
                <p className={g.prose}>
                  Grid rows 0fr → 1fr on one container, and a fade. The content stays mounted.
                </p>
              </Disclosure>
            </div>
          </Panel>
        </div>

        <div className={g.grid}>
          <Panel>
            <div className={g.body}>
              <Callsign title="Loaders" />
              <Loader kind="acquire" label="Acquiring sessions" />
              <Loader kind="steps" label="Planning" />
              <Loader kind="radar" label="Waiting for the relay" />
              <Loader kind="skeleton" label="Loading rows" />
            </div>
          </Panel>
          <Panel>
            <div className={g.body}>
              <Callsign title="Streaming" value="never held back" />
              <p className={g.prose} data-testid="g-stream">
                <StreamText text={stream.text} streaming={stream.started && stream.text.length < STREAM.length} />
              </p>
              <div className={g.row}>
                <button type="button" className={btn.ghost} onClick={stream.start}>
                  Stream an answer
                </button>
              </div>
            </div>
          </Panel>
          <Panel>
            <div className={g.body}>
              <Callsign title="Arrival" value="rows" />
              <div className={g.feed} data-testid="g-feed">
                {rows.map((r, i) => (
                  <div key={r} className={`${g.feedRow} ${i === 0 ? "nj-arrive nj-fade-ok" : ""}`}>
                    {r}
                  </div>
                ))}
              </div>
              <div className={g.row}>
                <button
                  type="button"
                  className={btn.ghost}
                  onClick={() => setRows((x) => [`UNIT-${rowId.current++} // arrived`, ...x].slice(0, 5))}
                >
                  A unit arrives
                </button>
              </div>
            </div>
          </Panel>
        </div>

        <Panel powerOn key={powerKey} data-testid="g-panel-power">
          <div className={g.body}>
            <Callsign title="Power-on" value="fade + rise + scan" meta="never scaled" />
            <p className={g.prose}>This panel powers on when it mounts.</p>
            <div className={g.row}>
              <button type="button" className={btn.ghost} onClick={() => setPowerKey((k) => k + 1)}>
                Power on again
              </button>
            </div>
          </div>
        </Panel>
      </div>
    </main>
  );
}
