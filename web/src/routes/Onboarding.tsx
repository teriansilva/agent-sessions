import encodeQR from "@paulmillr/qr";
import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Check,
  ExternalLink,
  FolderPlus,
  ShieldCheck,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useConfig, useConfigRefresh } from "../app/config";
import { api } from "../lib/api";
import { consentSaveError, saveAnalyticsConsent } from "../lib/analyticsConsent";
import { DOCS_HOME_URL } from "../lib/links";
import { EnableLoginDetails } from "../components/EnableLoginDetails";
import { mintNewSessionId } from "../lib/newSession";
import { engineInfo, mintsOwnId, useEngineRoster } from "../app/engineRoster";
import type { EngineInfo, Folder, TwoFactorEnrollment } from "../types/api";
import styles from "./Onboarding.module.css";
import { AiEndpointSetup } from "./AiEndpointSetup";
import { SlideCounter, SlideDots } from "../components/slideshow/Slideshow";
import { useSlideshow } from "../components/slideshow/useSlideshow";
import { newestRelease, whatsNewLabel } from "../whatsnew/due";

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
    title: "Every agent, one deck",
    body: "Every agent CLI on this machine gets its own persistent sessions in the sidebar, grouped by project, each with its agent's badge. Search, filter, favorite, archive.",
  },
  {
    img: publicAsset("whatsnew/0.20/missions.svg"),
    title: "Mission control",
    body: "Say the outcome you want. Mission control proposes a plan — a project, an agent and the objectives that define done — starts the agent when you press Begin, settles each objective on evidence and asks when it is unsure.",
  },
  {
    img: publicAsset("whatsnew/0.20/files.svg"),
    title: "Files, git & editing",
    body: "Every session has a Files panel: upload files or whole folders, edit a file right where you read it, and fetch, pull, stage, commit and push from its Git tab. A save never overwrites what the agent wrote meanwhile.",
  },
  {
    img: publicAsset("whatsnew/0.20/templates.svg"),
    title: "Prompt Templates",
    body: "Keep reusable instructions with fill-in fields, tags and images. Pick one from any composer — or save a good prompt as a template — and send it to any session.",
  },
  {
    img: publicAsset("onboarding/aireview.svg"),
    title: "AI session reviews",
    body: "An AI reads every running session and posts a one-line summary on each row, flagging the ones that need you — triage the whole fleet without scrolling transcripts. Point it at any OpenAI-compatible endpoint, even a local model.",
  },
  {
    img: publicAsset("onboarding/overview.svg"),
    title: "Tactical map & auto-sort",
    body: "A live flowchart of every session, grouped into projects — drag a session onto a project to reassign it. New sessions file themselves: AI auto-sort matches each one to the right project, or leaves it for you.",
  },
  {
    img: publicAsset("onboarding/mobile.svg"),
    title: "Touch-ready",
    body: "A mobile-first terminal with a real compose bar, control keys and image paste, plus console-style scroll-up history that survives reboots and deploys. Drive your whole fleet from a phone.",
  },
  {
    img: publicAsset("onboarding/homefree.svg"),
    title: "Home Free — from anywhere",
    body: "Your deck can stream through our blind relay — open battlelab.superstatus.io/connect in any browser and enter the console name + access key from your install. End-to-end encrypted; sessions run up to 4 hours.",
  },
  {
    img: publicAsset("onboarding/qrsignin.svg"),
    title: "Sign in by QR",
    body: "Add a phone or tablet without retyping a password: scan a QR from a signed-in device to authorize the new one. Optional TOTP two-factor keeps the login yours.",
  },
  {
    img: publicAsset("onboarding/settings.svg"),
    title: "Tune it in Settings",
    body: "Your AI endpoint, auto-sort, themes, security and more — all in Settings. The ? in the top bar brings back this tour (and a full setup re-run), the documentation and what's new, any time.",
  },
];

const WIZARD_STEPS = [
  "welcome",
  "security",
  "agents",
  "ai",
  "project",
  "tour",
  "analytics",
  "launch",
] as const;
type Step = (typeof WIZARD_STEPS)[number];
const STEP_LABEL: Record<Step, string> = {
  welcome: "Welcome",
  security: "Secure your deck",
  agents: "Connected agents",
  ai: "Set up AI",
  project: "First project",
  tour: "Tour",
  analytics: "Usage analytics",
  launch: "Launch",
};

/** Slideshow shared by the wizard's Tour step and the standalone replay (Help). Position, ←/→ and
 *  the dots come from the shared slideshow (#971), which What's new uses too. */
function Slideshow({
  onDone,
  doneLabel,
  onRerun,
  onWhatsNew,
}: {
  onDone: () => void;
  doneLabel: string;
  onRerun?: () => void;
  onWhatsNew?: () => void;
}) {
  const show = useSlideshow(SLIDES.length);
  const s = SLIDES[show.index];
  const whatsNew = onWhatsNew ? whatsNewLabel() : null;
  return (
    <div className={styles.tour}>
      <img className={styles.shot} src={s.img} alt="" />
      <div aria-live="polite" aria-atomic="true">
        <h3 className={styles.tourTitle}>{s.title}</h3>
        <p className={styles.tourBody}>{s.body}</p>
      </div>
      <div className={styles.tourNav}>
        <SlideDots state={show} />
        <SlideCounter state={show} />
      </div>
      <div className={styles.foot}>
        <button type="button" className={styles.ghost} onClick={onDone}>
          Skip
        </button>
        {onRerun && (
          <button type="button" className={styles.ghost} onClick={onRerun}>
            Re-run full setup
          </button>
        )}
        {onWhatsNew && whatsNew && (
          <button type="button" className={styles.ghost} onClick={onWhatsNew}>
            {whatsNew}
          </button>
        )}
        <span className={styles.grow} />
        {!show.first && (
          <button type="button" className={styles.btn} onClick={show.back}>
            <ArrowLeft size={14} /> Back
          </button>
        )}
        <button
          type="button"
          className={`${styles.pri} shine`}
          onClick={() => (show.last ? onDone() : show.next())}
        >
          {show.last ? doneLabel : "Next"} <ArrowRight size={14} />
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
  onRerunSetup,
  onWhatsNew,
}: {
  mode?: "wizard" | "tour";
  onClose: () => void;
  onRerunSetup?: () => void;
  /** Tour only (#971): open What's new from the tour's footer. */
  onWhatsNew?: () => void;
}) {
  const config = useConfig();
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>("welcome");
  // The SHARED roster decides whether a launch can mint its id (#853 P4, Hermes on #1134): this
  // wizard's own engine discovery can succeed while the roster is still loading or failed, and a
  // Launch button that is enabled but cannot mint would do nothing on click. One predicate drives
  // both the button and the click, and the wait says why.
  const roster = useEngineRoster();

  // Agents (Connected agents step).
  const [engines, setEngines] = useState<EngineInfo[] | null>(null);
  // Security step (#675) — optional TOTP 2FA, reusing the /api/2fa/* enroll→confirm flow.
  // N/A when there is no login (auth_mode=none, e.g. Home Free stream mode).
  const authMode = config?.auth_mode ?? "single-user";
  const loginOff = authMode === "none";
  const [twofaEnroll, setTwofaEnroll] = useState<TwoFactorEnrollment | null>(
    null,
  );
  const [twofaCode, setTwofaCode] = useState("");
  // Seed from the live config so a re-run (Help → Re-run setup) by someone who already has 2FA
  // enabled shows it as ON — never offers a re-enrollment the server rejects without fresh proof.
  const [twofaOn, setTwofaOn] = useState(!!config?.two_factor_enabled);
  const [twofaBusy, setTwofaBusy] = useState(false);
  const [twofaErr, setTwofaErr] = useState<string | null>(null);
  // Client-side QR from the otpauth:// URI (bundled lib, no CDN) — same as Settings' 2FA card.
  const twofaQr = useMemo(
    () =>
      twofaEnroll
        ? encodeQR(twofaEnroll.otpauth_uri, "svg", { border: 2 })
        : null,
    [twofaEnroll],
  );
  // Project + folder.
  const [folders, setFolders] = useState<Folder[]>([]);
  const [home, setHome] = useState("");
  const [cwd, setCwd] = useState("");
  const [projectName, setProjectName] = useState("");
  const [newFolder, setNewFolder] = useState("");
  const [folderErr, setFolderErr] = useState<string | null>(null);
  // Launch.
  // Onboarding launches a terminal session: API and chat agents (#1209, #1311) are created on the
  // server from New session instead, so they are not offered here.
  const newEngines = useMemo(
    () =>
      (config?.new_session_engines ?? []).filter((id) => {
        const runtime = engineInfo(id)?.runtime;
        return runtime !== "api" && runtime !== "chat";
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-run when the roster lands
    [config, roster.loaded],
  );
  const [engineChoice, setEngineChoice] = useState("");
  const engine = engineChoice || newEngines[0] || "";
  // #681: the folder <select> visibly shows its first option, but raw `cwd` stays "" until the
  // user picks (or a starred default_project seeds it). Derive an effective folder — the first
  // discovered one as a fallback, exactly like `engine` — so the select value, the launch/create
  // guards, and the launched payload all agree with what's on screen. An explicit selection
  // (setCwd) still wins, since a non-empty `cwd` short-circuits the fallback.
  const effectiveCwd = cwd || folders[0]?.cwd || "";
  const [bypass, setBypass] = useState(true);
  const [busy, setBusy] = useState(false);
  // Usage analytics (#1009). The step exists only when the server supports it and allows it
  // (`available` is false under AGENT_SESSIONS_ANALYTICS=0). The toggle is seeded once, below.
  const refreshConfig = useConfigRefresh();
  const analyticsStep = !!config?.analytics?.available;
  const analyticsDecided = !!config?.analytics?.decided;
  const [shareAnalytics, setShareAnalytics] = useState(false);
  const [analyticsBusy, setAnalyticsBusy] = useState(false);
  const [analyticsErr, setAnalyticsErr] = useState<string | null>(null);
  const steps = useMemo(
    () => WIZARD_STEPS.filter((s) => s !== "analytics" || analyticsStep),
    [analyticsStep],
  );

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
    if (config.default_project) setCwd(config.default_project);
    // A stored decision wins; undecided starts UNTICKED, on a fresh install and on a replay alike.
    // The product owner's call on #1009: consent is a box the operator ticks, never one pre-ticked.
    const a = config.analytics;
    if (a) setShareAnalytics(a.decided && a.enabled);
  }

  const finish = useCallback(async () => {
    try {
      await api.completeOnboarding(newestRelease()?.version);
    } catch {
      /* non-fatal — the gate also infers; a retry happens on the next config load */
    }
    onClose();
  }, [onClose]);

  const startTwofa = async () => {
    setTwofaBusy(true);
    setTwofaErr(null);
    try {
      setTwofaEnroll(await api.enroll2fa());
    } catch {
      setTwofaErr("Couldn't start enrollment.");
    } finally {
      setTwofaBusy(false);
    }
  };

  const confirmTwofa = async () => {
    const code = twofaCode.trim();
    if (!code) return;
    setTwofaBusy(true);
    setTwofaErr(null);
    try {
      await api.confirm2fa(code);
      setTwofaOn(true);
      setTwofaEnroll(null);
      setTwofaCode("");
    } catch {
      setTwofaErr(
        "That code didn't match — check your authenticator and try again.",
      );
    } finally {
      setTwofaBusy(false);
    }
  };

  const createFolder = async () => {
    const name = newFolder.trim();
    if (!name || !home) return;
    setFolderErr(null);
    try {
      const r = await api.fsMkdir(home, name);
      setFolders((f) => [
        { cwd: r.path, label: name },
        ...f.filter((x) => x.cwd !== r.path),
      ]);
      setCwd(r.path);
      setNewFolder("");
    } catch {
      setFolderErr("Couldn't create that folder.");
    }
  };

  const goPastProject = async () => {
    if (projectName.trim() && effectiveCwd) {
      try {
        await api.createProject({
          name: projectName.trim(),
          folders: [effectiveCwd],
          default_folder: effectiveCwd,
        });
      } catch {
        /* non-fatal: a name clash / adoption conflict shouldn't block onboarding */
      }
    }
    setStep("tour");
  };

  const launchReady = !!engine && roster.loaded && mintsOwnId(engine) !== undefined;
  const launchWait = !engine
    ? null
    : !roster.loaded
      ? roster.status === "failed"
        ? "Couldn’t load the agent list — retrying. Launch becomes available when it loads."
        : "Loading the agent list…"
      : mintsOwnId(engine) === undefined
        ? "This agent isn’t in the loaded agent list, so a session can’t be started for it."
        : null;
  const launch = async () => {
    // The engine's id mode must be KNOWN before anything is minted (#853 P4, #454).
    if (!engine || !effectiveCwd || !launchReady) return;
    setBusy(true);
    try {
      await api.completeOnboarding(newestRelease()?.version);
    } catch {
      /* non-fatal */
    }
    onClose();
    const id = mintNewSessionId(engine) ?? "";
    navigate(`/s/${engine}/${id}`, {
      state: { fresh: { cwd: effectiveCwd, bypass } },
    });
  };

  // Standalone slideshow replay (topbar Help) — no setup, no persistence.
  if (mode === "tour") {
    return (
      <Overlay onClose={onClose} title="Tour">
        <Slideshow
          onDone={onClose}
          doneLabel="Done"
          onRerun={onRerunSetup}
          onWhatsNew={onWhatsNew}
        />
      </Overlay>
    );
  }

  const idx = steps.indexOf(step);

  const saveAnalytics = async () => {
    setAnalyticsBusy(true);
    setAnalyticsErr(null);
    const r = await saveAnalyticsConsent(shareAnalytics);
    setAnalyticsBusy(false);
    if (r.ok) {
      // The refreshed config fetch is also what lets a new install count on its first day.
      refreshConfig();
      setStep("launch");
      return;
    }
    setAnalyticsErr(
      r.state
        ? `${consentSaveError(r.state)} Try again, or close setup and change it in Settings.`
        : `${consentSaveError(null)} Check it in Settings → Usage analytics.`,
    );
  };

  return (
    <Overlay onClose={finish} title="Set up BattleLab" wide>
      <nav className={styles.rail} aria-label="Setup steps">
        <span className={styles.railHead}>SETUP</span>
        {steps.map((s, n) => (
          <span
            key={s}
            className={`${styles.step} ${n === idx ? styles.cur : ""} ${n < idx ? styles.done : ""}`}
          >
            <span className={styles.stepDot}>
              {n < idx ? <Check size={11} /> : n + 1}
            </span>
            {STEP_LABEL[s]}
          </span>
        ))}
      </nav>

      <div className={styles.pane}>
        {step === "welcome" && (
          <Step
            title="Welcome to BattleLab"
            desc="Command & Code. A quick setup, then you're in."
          >
            <p className={styles.copy}>
              We'll check which agents are installed, let you wire up your AI,
              create a first project, and launch your first session — about a
              minute.
            </p>
            <p className={styles.copy}>
              Installed with streaming (the default)? Your deck is also
              reachable from any browser via the Connect page — end-to-end
              encrypted, sessions up to 4 hours.
            </p>
            <p className={styles.copy}>
              Every feature is documented, along with the security model and the
              CLI and API reference, at{" "}
              <a href={DOCS_HOME_URL} target="_blank" rel="noopener noreferrer">
                {new URL(DOCS_HOME_URL).host}
              </a>
              .
            </p>
            <Foot>
              <button type="button" className={styles.ghost} onClick={finish}>
                Skip setup
              </button>
              {/* A new tab, so the wizard and its progress stay exactly where they are (#987). */}
              <a
                className={styles.ghost}
                style={{ textDecoration: "none" }}
                href={DOCS_HOME_URL}
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Docs (opens in a new tab)"
              >
                <BookOpen size={14} aria-hidden="true" /> Docs{" "}
                <ExternalLink size={12} aria-hidden="true" />
              </a>
              <span className={styles.grow} />
              <button
                type="button"
                className={`${styles.pri} shine`}
                onClick={() => setStep("security")}
              >
                Get started <ArrowRight size={14} />
              </button>
            </Foot>
          </Step>
        )}

        {step === "security" && (
          <Step
            title="Secure your deck"
            desc="This box launches agents with permission bypass — treat it like SSH."
          >
            {loginOff ? (
              <>
                <div
                  className={styles.row}
                  style={{ alignItems: "flex-start" }}
                >
                  <Brackets />
                  <ShieldCheck size={16} aria-hidden="true" />
                  <div>
                    <strong>Login is off — your access key is the gate</strong>
                    <p className={styles.copy} style={{ margin: "4px 0 0" }}>
                      You installed with streaming (Home Free), so the app is
                      bound to loopback and reached only through the blind relay
                      with the access key the installer printed. No in-app
                      password is needed.
                    </p>
                  </div>
                </div>
                <EnableLoginDetails />
                <Foot>
                  <span className={styles.grow} />
                  <button
                    type="button"
                    className={styles.btn}
                    onClick={() => setStep("welcome")}
                  >
                    <ArrowLeft size={14} /> Back
                  </button>
                  <button
                    type="button"
                    className={`${styles.pri} shine`}
                    onClick={() => setStep("agents")}
                  >
                    Skip — continue <ArrowRight size={14} />
                  </button>
                </Foot>
              </>
            ) : (
              <>
                <p className={styles.copy}>
                  Your password is set. Add two-factor authentication (TOTP) for
                  a second layer — optional, and changeable anytime in Settings.
                </p>
                {twofaOn ? (
                  <p className={styles.note}>
                    <Check size={13} /> Two-factor authentication is on.
                  </p>
                ) : !twofaEnroll ? (
                  <button
                    type="button"
                    className={styles.btn}
                    onClick={startTwofa}
                    disabled={twofaBusy}
                  >
                    <ShieldCheck size={14} /> Add two-factor authentication
                  </button>
                ) : (
                  <div
                    className={styles.row}
                    style={{ alignItems: "flex-start" }}
                  >
                    <Brackets />
                    {twofaQr && (
                      <img
                        className={styles.qr}
                        src={`data:image/svg+xml,${encodeURIComponent(twofaQr)}`}
                        alt="Scan this QR with your authenticator app"
                      />
                    )}
                    <div style={{ flex: 1 }}>
                      <p className={styles.copy} style={{ margin: "0 0 6px" }}>
                        Scan with your authenticator, then enter a code to
                        confirm.
                      </p>
                      <p
                        className={styles.copy}
                        style={{ margin: "0 0 6px", fontSize: 11 }}
                      >
                        Recovery codes (save these once):
                        <br />
                        <code>{twofaEnroll.recovery_codes.join("  ")}</code>
                      </p>
                      <div className={styles.inline}>
                        <input
                          className={styles.field}
                          inputMode="numeric"
                          autoComplete="one-time-code"
                          placeholder="6-digit code"
                          value={twofaCode}
                          onChange={(e) => setTwofaCode(e.target.value)}
                        />
                        <button
                          type="button"
                          className={styles.btn}
                          onClick={confirmTwofa}
                          disabled={twofaBusy || !twofaCode.trim()}
                        >
                          Confirm 2FA
                        </button>
                      </div>
                    </div>
                  </div>
                )}
                {twofaErr && <p className={styles.err}>{twofaErr}</p>}
                <Foot>
                  <button
                    type="button"
                    className={styles.ghost}
                    onClick={() => setStep("agents")}
                  >
                    Skip for now
                  </button>
                  <span className={styles.grow} />
                  <button
                    type="button"
                    className={styles.btn}
                    onClick={() => setStep("welcome")}
                  >
                    <ArrowLeft size={14} /> Back
                  </button>
                  <button
                    type="button"
                    className={`${styles.pri} shine`}
                    onClick={() => setStep("agents")}
                  >
                    Continue <ArrowRight size={14} />
                  </button>
                </Foot>
              </>
            )}
          </Step>
        )}

        {step === "agents" && (
          <Step
            title="Connected agents"
            desc="Detected on this host. Install more, then re-open setup."
          >
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
                    <span className={styles.path}>
                      {e.bin ?? "install to enable"}
                    </span>
                    {e.present && (
                      <span
                        className={`${styles.badge} ${e.supports_new ? styles.badgeGo : ""}`}
                      >
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
              <button
                type="button"
                className={styles.btn}
                onClick={() => setStep("security")}
              >
                <ArrowLeft size={14} /> Back
              </button>
              <button
                type="button"
                className={`${styles.pri} shine`}
                onClick={() => setStep("ai")}
              >
                Next <ArrowRight size={14} />
              </button>
            </Foot>
          </Step>
        )}

        {step === "ai" && (
          <Step
            title="Set up your AI"
            desc="Powers session review, auto-sort and mission control. You provide it — an OpenAI-compatible endpoint: connect it, then pick the model."
          >
            <AiEndpointSetup variant="wizard" />
            <Foot>
              <button
                type="button"
                className={styles.ghost}
                onClick={() => setStep("project")}
              >
                I'll do this later
              </button>
              <span className={styles.grow} />
              <button
                type="button"
                className={styles.btn}
                onClick={() => setStep("agents")}
              >
                <ArrowLeft size={14} /> Back
              </button>
              <button
                type="button"
                className={`${styles.pri} shine`}
                onClick={() => setStep("project")}
              >
                Next <ArrowRight size={14} />
              </button>
            </Foot>
          </Step>
        )}

        {step === "project" && (
          <Step
            title="Create your first project"
            desc="Your home folder is the root. Start in one, or make a new folder."
          >
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
              <select
                value={effectiveCwd}
                onChange={(e) => setCwd(e.target.value)}
              >
                {folders.length === 0 && (
                  <option value="">no folders found</option>
                )}
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
              <button
                type="button"
                className={styles.btn}
                onClick={() => setStep("ai")}
              >
                <ArrowLeft size={14} /> Back
              </button>
              <button
                type="button"
                className={`${styles.pri} shine`}
                onClick={goPastProject}
              >
                Next <ArrowRight size={14} />
              </button>
            </Foot>
          </Step>
        )}

        {step === "tour" && (
          <Step
            title="The lay of the land"
            desc="A 30-second tour of the main surfaces."
          >
            <Slideshow
              onDone={() => setStep(analyticsStep ? "analytics" : "launch")}
              doneLabel="Finish tour"
            />
          </Step>
        )}

        {step === "analytics" && (
          <Step
            title="Usage analytics"
            desc="A daily active-install report tells us how many installs are in use. Your call."
          >
            <label className={styles.checkbox}>
              {/* Frozen while a save is in flight: the save carries the value captured when
                  Continue was pressed, so an edit made meanwhile would be silently dropped. */}
              <input
                type="checkbox"
                checked={shareAnalytics}
                disabled={analyticsBusy}
                onChange={(e) => setShareAnalytics(e.target.checked)}
              />
              <span>Share usage analytics</span>
            </label>
            <p className={styles.copy}>
              <b>What is sent</b> — a daily active-install report on days you
              open BattleLab, with up to three delivery attempts:
            </p>
            <ul className={`${styles.copy} ${styles.list}`}>
              <li>
                a random install ID — not derived from this machine, your
                account or your network
              </li>
              <li>the BattleLab version and your operating system</li>
            </ul>
            <p className={styles.copy}>
              <b>Never sent</b> — sessions, prompts, code, file or project
              names, hostnames, account details.
            </p>
            <p className={styles.copy}>
              <b>Where</b> — the BattleLab team&apos;s self-hosted Umami server.
              It uses your IP address to estimate an approximate location and
              does not store the address; the web server in front of it keeps
              standard access logs, which include it, for up to 52 days.
            </p>
            <p className={styles.copy}>
              {analyticsDecided
                ? "Your current setting stays in effect until you continue."
                : "Nothing is sent unless you tick the box and continue."}{" "}
              Change it any time in <b>Settings → Usage analytics</b>.
            </p>
            {analyticsErr && (
              <p className={styles.err} role="alert">
                {analyticsErr}
              </p>
            )}
            <Foot>
              <span className={styles.grow} />
              <button
                type="button"
                className={styles.btn}
                onClick={() => setStep("tour")}
                disabled={analyticsBusy}
              >
                <ArrowLeft size={14} /> Back
              </button>
              <button
                type="button"
                className={`${styles.pri} shine`}
                onClick={() => void saveAnalytics()}
                disabled={analyticsBusy}
              >
                {analyticsErr ? "Try again" : "Continue"}{" "}
                <ArrowRight size={14} />
              </button>
            </Foot>
          </Step>
        )}

        {step === "launch" && (
          <Step
            title="Start your first session"
            desc="Everything's set — launch into your project."
          >
            {newEngines.length > 1 && (
              <label className={styles.field}>
                <span>Agent</span>
                <select
                  value={engine}
                  onChange={(e) => setEngineChoice(e.target.value)}
                >
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
              <select
                value={effectiveCwd}
                onChange={(e) => setCwd(e.target.value)}
              >
                {folders.length === 0 && (
                  <option value="">no folder selected</option>
                )}
                {folders.map((f) => (
                  <option key={f.cwd} value={f.cwd}>
                    {f.label}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.checkbox}>
              <input
                type="checkbox"
                checked={bypass}
                onChange={(e) => setBypass(e.target.checked)}
              />
              <span>Skip permission prompts</span>
            </label>
            <p className={styles.copy}>
              Want to read first? The{" "}
              <a href={DOCS_HOME_URL} target="_blank" rel="noopener noreferrer">
                docs
              </a>{" "}
              cover every feature — and the ? in the top bar brings back the tour,
              the docs and What's new at any time.
            </p>
            {launchWait && (
              <p className={styles.note} role="status">
                {launchWait}
              </p>
            )}
            <Foot>
              <button type="button" className={styles.ghost} onClick={finish}>
                Finish without launching
              </button>
              <span className={styles.grow} />
              <button
                type="button"
                className={styles.btn}
                onClick={() => setStep(analyticsStep ? "analytics" : "tour")}
              >
                <ArrowLeft size={14} /> Back
              </button>
              <button
                type="button"
                className={`${styles.pri} shine`}
                onClick={launch}
                disabled={busy || !engine || !effectiveCwd || !launchReady}
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
  // Focus moves INTO the overlay (#987). A frame late on purpose: opened from the Help menu in the
  // phone drawer, the menu and the drawer close in the same commit and each restores its trigger a
  // frame later — a synchronous focus here would be undone by those restores. This frame is queued
  // after theirs, so it runs after them; and it leaves focus alone if it already arrived inside.
  const cardRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      const card = cardRef.current;
      if (card && !card.contains(document.activeElement)) card.focus();
    });
    return () => cancelAnimationFrame(id);
  }, []);
  return (
    <div
      className={styles.scrim}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        ref={cardRef}
        tabIndex={-1}
        className={`${styles.card} ${wide ? styles.wide : ""}`}
      >
        <Brackets hero />
        <header className={styles.bar}>
          <span className={styles.brand}>
            <span aria-hidden="true">◢</span> BATTLE<b>LAB</b>
          </span>
          <span className={styles.barTag}>{title.toUpperCase()}</span>
          <button
            type="button"
            className={styles.x}
            aria-label="Close"
            onClick={onClose}
          >
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
