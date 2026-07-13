import { ArrowLeft, ArrowRight, Check, FolderPlus, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useConfig } from "../app/config";
import { api } from "../lib/api";
import { mintNewSessionId } from "../lib/newSession";
import type { EngineInfo, Folder } from "../types/api";
import styles from "./Onboarding.module.css";

/** Corner-bracket frame (the `.hud-cnr` primitive in App.css). Inlined here — the shared
 *  `HudFrame` component lands on the HUD-compliance branch (#466); App.tsx inlines them too. */
function Brackets({ hero = false }: { hero?: boolean }) {
  const c = hero ? "hud-cnr hero" : "hud-cnr";
  return (
    <>
      <span className={`${c} tl`} aria-hidden="true" />
      <span className={`${c} tr`} aria-hidden="true" />
      <span className={`${c} bl`} aria-hidden="true" />
      <span className={`${c} br`} aria-hidden="true" />
    </>
  );
}

/** Slideshow tour slides — images are placeholders (web/public/onboarding/*.svg); the copy is
 *  the durable part. Real screenshots are a follow-up (#463 out-of-scope). */
const publicAsset = (path: string): string =>
  `${import.meta.env.BASE_URL}${path.replace(/^\/+/, "")}`;

const SLIDES: { img: string; title: string; body: string }[] = [
  {
    img: publicAsset("onboarding/sessions.svg"),
    title: "Your sessions",
    body: "Every agent session lives in the sidebar — search, filter by project or engine, favorite, archive.",
  },
  {
    img: publicAsset("onboarding/pulse.svg"),
    title: "Pulse",
    body: "An AI-curated read on what needs you, what's in flight, and what's gone idle. Open it from the topbar.",
  },
  {
    img: publicAsset("onboarding/overview.svg"),
    title: "Overview map",
    body: "Your projects and sessions as a flowchart — drag a session onto a project to reassign it.",
  },
  {
    img: publicAsset("onboarding/homefree.svg"),
    title: "Home Free — from anywhere",
    body: "Your deck can stream through our blind relay — open battlelab.superstatus.io/connect in any browser and enter the console name + access key from your install. End-to-end encrypted; sessions run up to 4 hours.",
  },
  {
    img: publicAsset("onboarding/settings.svg"),
    title: "Settings → AI",
    body: "Tune your AI endpoint, auto-sort, themes and more. Re-open this tour any time from Help.",
  },
];

const WIZARD_STEPS = ["welcome", "agents", "ai", "project", "tour", "launch"] as const;
type Step = (typeof WIZARD_STEPS)[number];
const STEP_LABEL: Record<Step, string> = {
  welcome: "Welcome",
  agents: "Connected agents",
  ai: "Set up AI",
  project: "First project",
  tour: "Tour",
  launch: "Launch",
};

/** Slideshow shared by the wizard's Tour step and the standalone replay (Help). */
function Slideshow({ onDone, doneLabel }: { onDone: () => void; doneLabel: string }) {
  const [i, setI] = useState(0);
  const last = i >= SLIDES.length - 1;
  const s = SLIDES[i];
  return (
    <div className={styles.tour}>
      <img className={styles.shot} src={s.img} alt="" />
      <h3 className={styles.tourTitle}>{s.title}</h3>
      <p className={styles.tourBody}>{s.body}</p>
      <div className={styles.dots} aria-hidden="true">
        {SLIDES.map((_, n) => (
          <i key={n} className={n === i ? styles.dotOn : ""} />
        ))}
      </div>
      <div className={styles.foot}>
        <button type="button" className={styles.ghost} onClick={onDone}>
          Skip
        </button>
        <span className={styles.grow} />
        {i > 0 && (
          <button type="button" className={styles.btn} onClick={() => setI((n) => n - 1)}>
            <ArrowLeft size={14} /> Back
          </button>
        )}
        <button
          type="button"
          className={`${styles.pri} shine`}
          onClick={() => (last ? onDone() : setI((n) => n + 1))}
        >
          {last ? doneLabel : "Next"} <ArrowRight size={14} />
        </button>
      </div>
    </div>
  );
}

/** First-run onboarding (#463). `mode="wizard"` is the gated setup flow; `mode="tour"` is the
 *  replayable slideshow (from the topbar Help entry). `onClose` dismisses the overlay — the
 *  wizard also persists `onboarded` (via the Launch/Finish/Skip actions) so it never returns. */
export function Onboarding({
  mode = "wizard",
  onClose,
}: {
  mode?: "wizard" | "tour";
  onClose: () => void;
}) {
  const config = useConfig();
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>("welcome");

  // Agents (Connected agents step).
  const [engines, setEngines] = useState<EngineInfo[] | null>(null);
  // AI (Set up AI step) — a compact view over the existing /api/prefs → ai_review contract.
  const [aiBase, setAiBase] = useState("");
  const [aiKey, setAiKey] = useState("");
  const [aiModel, setAiModel] = useState("");
  const [aiSaving, setAiSaving] = useState(false);
  const [aiSaved, setAiSaved] = useState(false);
  const [aiErr, setAiErr] = useState<string | null>(null);
  // Project + folder.
  const [folders, setFolders] = useState<Folder[]>([]);
  const [home, setHome] = useState("");
  const [cwd, setCwd] = useState("");
  const [projectName, setProjectName] = useState("");
  const [newFolder, setNewFolder] = useState("");
  const [folderErr, setFolderErr] = useState<string | null>(null);
  // Launch.
  const newEngines = useMemo(() => config?.new_session_engines ?? [], [config]);
  const [engineChoice, setEngineChoice] = useState("");
  const engine = engineChoice || newEngines[0] || "";
  const [bypass, setBypass] = useState(true);
  const [busy, setBusy] = useState(false);

  // Load engine discovery once we reach (or mount on) the wizard.
  useEffect(() => {
    if (mode !== "wizard") return;
    let alive = true;
    api
      .engines()
      .then((r) => alive && setEngines(r.engines))
      .catch(() => alive && setEngines([]));
    api
      .folders({ visible: true })
      .then((r) => alive && setFolders(r.folders))
      .catch(() => {});
    api
      .fsDirs()
      .then((r) => alive && setHome(r.home))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [mode]);

  // Seed AI fields + default cwd from config the first time it lands — a render-time
  // adjustment, not an effect (the "you might not need an effect" pattern, mirroring Pulse's
  // depth sync). React bails out and re-renders before committing, so there's no cascade.
  const [seeded, setSeeded] = useState(false);
  if (!seeded && config) {
    setSeeded(true);
    if (config.ai_review) {
      setAiBase(config.ai_review.base_url);
      setAiModel(config.ai_review.model);
    }
    if (config.default_project) setCwd(config.default_project);
  }

  const finish = useCallback(async () => {
    try {
      await api.completeOnboarding();
    } catch {
      /* non-fatal — the gate also infers; a retry happens on the next config load */
    }
    onClose();
  }, [onClose]);

  const saveAi = async () => {
    setAiSaving(true);
    setAiErr(null);
    try {
      await api.setPrefs({
        ai_review: { base_url: aiBase.trim(), api_key: aiKey, model: aiModel.trim() },
      });
      setAiSaved(true);
      setAiKey("");
    } catch {
      setAiErr("Couldn't save — check the endpoint URL and key.");
    } finally {
      setAiSaving(false);
    }
  };

  const createFolder = async () => {
    const name = newFolder.trim();
    if (!name || !home) return;
    setFolderErr(null);
    try {
      const r = await api.fsMkdir(home, name);
      setFolders((f) => [{ cwd: r.path, label: name }, ...f.filter((x) => x.cwd !== r.path)]);
      setCwd(r.path);
      setNewFolder("");
    } catch {
      setFolderErr("Couldn't create that folder.");
    }
  };

  const goPastProject = async () => {
    if (projectName.trim() && cwd) {
      try {
        await api.createProject({
          name: projectName.trim(),
          folders: [cwd],
          default_folder: cwd,
        });
      } catch {
        /* non-fatal: a name clash / adoption conflict shouldn't block onboarding */
      }
    }
    setStep("tour");
  };

  const launch = async () => {
    if (!engine || !cwd) return;
    setBusy(true);
    try {
      await api.completeOnboarding();
    } catch {
      /* non-fatal */
    }
    onClose();
    const id = mintNewSessionId(engine);
    navigate(`/s/${engine}/${id}`, { state: { fresh: { cwd, bypass } } });
  };

  // Standalone slideshow replay (topbar Help) — no setup, no persistence.
  if (mode === "tour") {
    return (
      <Overlay onClose={onClose} title="Tour">
        <Slideshow onDone={onClose} doneLabel="Done" />
      </Overlay>
    );
  }

  const idx = WIZARD_STEPS.indexOf(step);

  return (
    <Overlay onClose={finish} title="Set up BattleLab" wide>
      <nav className={styles.rail} aria-label="Setup steps">
        <span className={styles.railHead}>SETUP</span>
        {WIZARD_STEPS.map((s, n) => (
          <span
            key={s}
            className={`${styles.step} ${n === idx ? styles.cur : ""} ${n < idx ? styles.done : ""}`}
          >
            <span className={styles.stepDot}>{n < idx ? <Check size={11} /> : n + 1}</span>
            {STEP_LABEL[s]}
          </span>
        ))}
      </nav>

      <div className={styles.pane}>
        {step === "welcome" && (
          <Step title="Welcome to BattleLab" desc="Command & Code. A quick setup, then you're in.">
            <p className={styles.copy}>
              We'll check which agents are installed, let you wire up your AI, create a first
              project, and launch your first session — about a minute.
            </p>
            <p className={styles.copy}>
              Installed with streaming (the default)? Your deck is also reachable from any
              browser via the Connect page — end-to-end encrypted, sessions up to 4 hours.
            </p>
            <Foot>
              <button type="button" className={styles.ghost} onClick={finish}>
                Skip setup
              </button>
              <span className={styles.grow} />
              <button type="button" className={`${styles.pri} shine`} onClick={() => setStep("agents")}>
                Get started <ArrowRight size={14} />
              </button>
            </Foot>
          </Step>
        )}

        {step === "agents" && (
          <Step title="Connected agents" desc="Detected on this host. Install more, then re-open setup.">
            <ul className={styles.rows}>
              {engines === null ? (
                <li className={styles.skeleton}>Scanning…</li>
              ) : (
                engines.map((e) => (
                  <li key={e.id} className={styles.row}>
                    <Brackets />
                    <span className={styles.eng}>{e.id}</span>
                    {e.present ? (
                      <span className={styles.ok}>✓ ready</span>
                    ) : (
                      <span className={styles.na}>— not found</span>
                    )}
                    <span className={styles.path}>{e.bin ?? "install to enable"}</span>
                    {e.present && (
                      <span className={`${styles.badge} ${e.supports_new ? styles.badgeGo : ""}`}>
                        {e.supports_new ? "can start" : "resume only"}
                      </span>
                    )}
                  </li>
                ))
              )}
            </ul>
            <Foot>
              <button type="button" className={styles.ghost} onClick={finish}>
                Skip setup
              </button>
              <span className={styles.grow} />
              <button type="button" className={styles.btn} onClick={() => setStep("welcome")}>
                <ArrowLeft size={14} /> Back
              </button>
              <button type="button" className={`${styles.pri} shine`} onClick={() => setStep("ai")}>
                Next <ArrowRight size={14} />
              </button>
            </Foot>
          </Step>
        )}

        {step === "ai" && (
          <Step
            title="Set up your AI"
            desc="Powers AI Review, Auto-sort & Pulse. You provide it — an OpenAI-compatible endpoint."
          >
            <label className={styles.field}>
              <span>Endpoint base URL</span>
              <input
                value={aiBase}
                onChange={(e) => setAiBase(e.target.value)}
                placeholder="https://api.openai.com/v1"
                spellCheck={false}
              />
            </label>
            <label className={styles.field}>
              <span>API key {config?.ai_review?.api_key_set ? "(set — leave blank to keep)" : ""}</span>
              <input
                type="password"
                value={aiKey}
                onChange={(e) => setAiKey(e.target.value)}
                placeholder="sk-…  (write-only — never echoed)"
                autoComplete="off"
              />
            </label>
            <label className={styles.field}>
              <span>Model</span>
              <input
                value={aiModel}
                onChange={(e) => setAiModel(e.target.value)}
                placeholder="gpt-4o"
                spellCheck={false}
              />
            </label>
            {aiErr && <p className={styles.err}>{aiErr}</p>}
            {aiSaved && <p className={styles.note}>Saved. Fine-tune anytime in Settings → AI.</p>}
            <Foot>
              <button type="button" className={styles.ghost} onClick={() => setStep("project")}>
                I'll do this later
              </button>
              <span className={styles.grow} />
              <button type="button" className={styles.btn} onClick={() => setStep("agents")}>
                <ArrowLeft size={14} /> Back
              </button>
              <button
                type="button"
                className={styles.btn}
                onClick={saveAi}
                disabled={aiSaving || !aiBase.trim()}
              >
                {aiSaving ? "Saving…" : "Save & validate"}
              </button>
              <button type="button" className={`${styles.pri} shine`} onClick={() => setStep("project")}>
                Next <ArrowRight size={14} />
              </button>
            </Foot>
          </Step>
        )}

        {step === "project" && (
          <Step title="Create your first project" desc="Your home folder is the root. Start in one, or make a new folder.">
            <label className={styles.field}>
              <span>Project name (optional)</span>
              <input
                value={projectName}
                onChange={(e) => setProjectName(e.target.value)}
                placeholder="BattleLab Ops"
              />
            </label>
            <label className={styles.field}>
              <span>Launch folder</span>
              <select value={cwd} onChange={(e) => setCwd(e.target.value)}>
                {folders.length === 0 && <option value="">no folders found</option>}
                {folders.map((f) => (
                  <option key={f.cwd} value={f.cwd}>
                    {f.label}
                  </option>
                ))}
              </select>
            </label>
            <div className={styles.newFolder}>
              <input
                value={newFolder}
                onChange={(e) => setNewFolder(e.target.value)}
                placeholder={home ? `new folder under ${home}` : "new folder"}
                spellCheck={false}
              />
              <button
                type="button"
                className={styles.btn}
                onClick={createFolder}
                disabled={!newFolder.trim() || !home}
              >
                <FolderPlus size={14} /> Create
              </button>
            </div>
            {folderErr && <p className={styles.err}>{folderErr}</p>}
            <Foot>
              <span className={styles.grow} />
              <button type="button" className={styles.btn} onClick={() => setStep("ai")}>
                <ArrowLeft size={14} /> Back
              </button>
              <button type="button" className={`${styles.pri} shine`} onClick={goPastProject}>
                Next <ArrowRight size={14} />
              </button>
            </Foot>
          </Step>
        )}

        {step === "tour" && (
          <Step title="The lay of the land" desc="A 30-second tour of the main surfaces.">
            <Slideshow onDone={() => setStep("launch")} doneLabel="Finish tour" />
          </Step>
        )}

        {step === "launch" && (
          <Step title="Start your first session" desc="Everything's set — launch into your project.">
            {newEngines.length > 1 && (
              <label className={styles.field}>
                <span>Agent</span>
                <select value={engine} onChange={(e) => setEngineChoice(e.target.value)}>
                  {newEngines.map((id) => (
                    <option key={id} value={id}>
                      {id}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className={styles.field}>
              <span>Folder</span>
              <select value={cwd} onChange={(e) => setCwd(e.target.value)}>
                {folders.length === 0 && <option value="">no folder selected</option>}
                {folders.map((f) => (
                  <option key={f.cwd} value={f.cwd}>
                    {f.label}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.checkbox}>
              <input type="checkbox" checked={bypass} onChange={(e) => setBypass(e.target.checked)} />
              <span>Skip permission prompts</span>
            </label>
            <Foot>
              <button type="button" className={styles.ghost} onClick={finish}>
                Finish without launching
              </button>
              <span className={styles.grow} />
              <button type="button" className={styles.btn} onClick={() => setStep("tour")}>
                <ArrowLeft size={14} /> Back
              </button>
              <button
                type="button"
                className={`${styles.pri} shine`}
                onClick={launch}
                disabled={busy || !engine || !cwd}
              >
                ⮞ Launch session
              </button>
            </Foot>
          </Step>
        )}
      </div>
    </Overlay>
  );
}

function Overlay({
  children,
  onClose,
  title,
  wide,
}: {
  children: React.ReactNode;
  onClose: () => void;
  title: string;
  wide?: boolean;
}) {
  // Esc closes (skips) the overlay.
  useEffect(() => {
    const on = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [onClose]);
  return (
    <div className={styles.scrim} role="dialog" aria-modal="true" aria-label={title}>
      <div className={`${styles.card} ${wide ? styles.wide : ""}`}>
        <Brackets hero />
        <header className={styles.bar}>
          <span className={styles.brand}>
            <span aria-hidden="true">◢</span> BATTLE<b>LAB</b>
          </span>
          <span className={styles.barTag}>{title.toUpperCase()}</span>
          <button type="button" className={styles.x} aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>
        <div className={styles.cardBody}>{children}</div>
      </div>
    </div>
  );
}

function Step({
  title,
  desc,
  children,
}: {
  title: string;
  desc: string;
  children: React.ReactNode;
}) {
  return (
    <div className={styles.stepPane}>
      <h2 className={styles.h2}>{title}</h2>
      <p className={styles.desc}>{desc}</p>
      <div className={styles.stepBody}>{children}</div>
    </div>
  );
}

function Foot({ children }: { children: React.ReactNode }) {
  return <div className={styles.footRow}>{children}</div>;
}
