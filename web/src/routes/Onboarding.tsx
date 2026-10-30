import encodeQR from "@paulmillr/qr";
import { SessionCapacityFactsRow } from "../components/settings/ResourceLimits";
import { capacityReason } from "../lib/sessionLimit";
import type { ResourceSettings } from "../types/resources";
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
import {
  clientChoices,
  engineInfo,
  engineLabel,
  mintsOwnId,
  useEngineRoster,
} from "../app/engineRoster";
import type { Folder, TwoFactorEnrollment } from "../types/api";
import type { PluginCatalog } from "../types/plugins";
import { ApiError } from "../lib/api";
import { createStructuredSession, startCreated } from "../lib/structuredCreate";
import { openFullScreen } from "../lib/newSessionLaunch";
import { pluginError } from "../components/plugins/model";
import { SetupAgents } from "../components/onboarding/SetupAgents";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import styles from "./Onboarding.module.css";
import { AiEndpointSetup } from "./AiEndpointSetup";
import { HqContactGallery } from "../components/hq/HqContactGallery";
import { SlideCounter, SlideDots } from "../components/slideshow/Slideshow";
import { useSlideshow } from "../components/slideshow/useSlideshow";
import { newestRelease, whatsNewLabel } from "../whatsnew/due";
import { BrandMark } from "../components/brand/Brand";

/** Slideshow tour slides (#1544): real captures of the app (`web/visual/promo/capture.mjs`), plus
 *  two drawn slides for surfaces a capture can't show (the relay, Settings). The copy is the durable
 *  part; re-run the capture when a surface changes. */
const publicAsset = (path: string): string =>
  `${import.meta.env.BASE_URL}${path.replace(/^\/+/, "")}`;

const SLIDES: { img: string; title: string; body: string }[] = [
  {
    img: publicAsset("onboarding/dashboard.webp"),
    title: "Welcome, Commander",
    body: "The dashboard is your start page: what needs you, what's running, your missions and how much quota each agent has left — the whole operation on one screen.",
  },
  {
    img: publicAsset("onboarding/hq.webp"),
    title: "HQ — your General on the line",
    body: "Ask HQ what needs you, have it start or nudge sessions, and follow up on what it started. Your primary contact — a General you pick — answers in their own voice. HQ runs on your own AI endpoint.",
  },
  {
    img: publicAsset("onboarding/sessions.webp"),
    title: "Every agent, one deck",
    body: "Claude Code, Codex, opencode, Gemini, Antigravity and Kimi Code each get persistent sessions, grouped by project: terminal sessions that survive reboots and deploys, or API sessions with a structured view, approvals and a message queue.",
  },
  {
    img: publicAsset("onboarding/missions.webp"),
    title: "Missions (beta)",
    body: "Give an objective. Mission control proposes a plan — a project, an agent and the objectives that define done — runs it when you say go, settles each objective on evidence and asks when it is unsure. Beta: still settling, expect changes.",
  },
  {
    img: publicAsset("onboarding/library.webp"),
    title: "The Library",
    body: "Playbooks, prompt templates, variables, memory, automations and skills — how your work gets done, reusable from every session and mission.",
  },
  {
    img: publicAsset("onboarding/files.webp"),
    title: "Files, git & editing",
    body: "Every session has a Files panel: upload files or whole folders, edit a file right where you read it, and fetch, pull, stage, commit and push from its Git tab. A save never overwrites what the agent wrote meanwhile.",
  },
  {
    img: publicAsset("onboarding/map.webp"),
    title: "Tactical map & auto-sort",
    body: "A live flowchart of every session, grouped into projects — open several agents side by side and drag a session onto a project to reassign it. AI auto-sort files new sessions for you.",
  },
  {
    img: publicAsset("onboarding/phone.webp"),
    title: "Command from your phone",
    body: "A mobile-first terminal with a real compose bar, control keys, dictation and image paste, plus scroll-up history that survives reboots and deploys. Drive the whole fleet from a phone.",
  },
  {
    img: publicAsset("onboarding/homefree.svg"),
    title: "Home Free — from anywhere",
    body: "Your deck can stream through our blind relay — open battlelab.superstatus.io/connect in any browser and enter the console name + access key from your install. End-to-end encrypted; sessions run up to 4 hours.",
  },
  {
    img: publicAsset("onboarding/settings.svg"),
    title: "Tune it in Settings",
    body: "Your AI endpoint, your HQ contact, appearance, agents, two-factor and QR sign-in — all in Settings. The operator menu at the top right brings back this tour (and a full setup re-run), the documentation and What's new, any time.",
  },
];

const WIZARD_STEPS = [
  "welcome",
  "security",
  "agents",
  "system",
  "ai",
  "contact",
  "project",
  "tour",
  "analytics",
  "launch",
] as const;
type Step = (typeof WIZARD_STEPS)[number];
const STEP_LABEL: Record<Step, string> = {
  welcome: "Welcome",
  security: "Secure your deck",
  agents: "Agents",
  system: "Your system",
  ai: "Set up AI",
  contact: "HQ contact",
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
  // The Agents step stays mounted once visited (see its render): render-time, like `seeded` below.
  const [agentsSeen, setAgentsSeen] = useState(false);
  if (step === "agents" && !agentsSeen) setAgentsSeen(true);
  // The SHARED roster decides whether a launch can mint its id (#853 P4, Hermes on #1134): this
  // wizard's own engine discovery can succeed while the roster is still loading or failed, and a
  // Launch button that is enabled but cannot mint would do nothing on click. One predicate drives
  // both the button and the click, and the wait says why.
  const roster = useEngineRoster();

  // Agents step (#1739): the agent catalog, read when the wizard opens and after every inline setup.
  const [catalog, setCatalog] = useState<PluginCatalog | null>(null);
  const [catalogErr, setCatalogErr] = useState<string | null>(null);
  // Reads overlap (the wizard opening, every return to Agents, every inline close): only the NEWEST
  // read's answer counts, so an older success can never undo a newer failure; none lands after
  // unmount.
  const catalogGen = useRef(0);
  useEffect(
    () => () => {
      catalogGen.current += 1;
    },
    [],
  );
  const loadCatalog = useCallback(() => {
    const gen = ++catalogGen.current;
    api
      .plugins()
      .then((c) => {
        if (gen !== catalogGen.current) return;
        setCatalog(c);
        setCatalogErr(null);
      })
      .catch((e) => {
        if (gen === catalogGen.current) setCatalogErr(pluginError(e));
      });
  }, []);
  // Re-read on every return to the Agents step: an operation started there may have moved on.
  useEffect(() => {
    if (mode === "wizard" && step === "agents") loadCatalog();
  }, [mode, step, loadCatalog]);
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
  // Launch — API first (#1739): a ready API client is preselected and its session is created on the
  // server; a console client launches a terminal as before; an API client that cannot start is
  // listed with its reason. ONE rule decides, shared with the rest of the app (`clientChoices`).
  // Chat clients (an endpoint, #1209) are started from New session, not here.
  const choices = useMemo(
    () =>
      clientChoices(
        roster.engines,
        config?.new_session_engines ?? [],
        config?.unavailable_clients ?? [],
      ),
    [roster.engines, config],
  );
  const [engineChoice, setEngineChoice] = useState("");
  // A choice that is no longer startable (the roster changed under it) falls back to the default.
  const engine =
    (engineChoice && [...choices.api, ...choices.terminal].includes(engineChoice)
      ? engineChoice
      : choices.preselect) ?? "";
  const launchApi = engineInfo(engine)?.runtime === "api";
  // Only a client whose adapter maps "Skip permission prompts" can skip (#1339); the server re-checks.
  const launchCanBypass = !launchApi || engineInfo(engine)?.api?.can_bypass === true;
  const [launchErr, setLaunchErr] = useState<string | null>(null);
  // #681: the folder <select> visibly shows its first option, but raw `cwd` stays "" until the
  // user picks (or a starred default_project seeds it). Derive an effective folder — the first
  // discovered one as a fallback, exactly like `engine` — so the select value, the launch/create
  // guards, and the launched payload all agree with what's on screen. An explicit selection
  // (setCwd) still wins, since a non-empty `cwd` short-circuits the fallback.
  const effectiveCwd = cwd || folders[0]?.cwd || "";
  // Skip permission prompts. A choice made on this form is bound to the client it was made for, so
  // it never carries from a terminal client into an API client. Without one, a terminal launch
  // skips (the wizard's long-standing default), but an API client FAILS CLOSED exactly like New
  // session (#1339/#1473): it skips only when a LOADED config says the stored default is on.
  const [bypassChoice, setBypassChoice] = useState<{ engine: string; value: boolean } | null>(
    null,
  );
  const bypass = launchApi
    ? launchCanBypass &&
      (bypassChoice?.engine === engine
        ? bypassChoice.value
        : config?.agent_defaults?.bypass === true)
    : bypassChoice?.engine === engine
      ? bypassChoice.value
      : true;
  const setBypass = (value: boolean) => setBypassChoice({ engine, value });
  const [busy, setBusy] = useState(false);
  // Usage analytics (#1009). The step exists only when the server supports it and allows it
  // (`available` is false under AGENT_SESSIONS_ANALYTICS=0). The toggle is seeded once, below.
  const refreshConfig = useConfigRefresh();
  const analyticsStep = !!config?.analytics?.available;
  const analyticsDecided = !!config?.analytics?.decided;
  const [shareAnalytics, setShareAnalytics] = useState(false);
  const [analyticsBusy, setAnalyticsBusy] = useState(false);
  const [analyticsErr, setAnalyticsErr] = useState<string | null>(null);
  // The HQ contact step (#1544) exists only once an AI endpoint is configured: without one there is
  // no HQ to answer, so a contact would be a face with nothing to say. Live: saving the endpoint in
  // the AI step refreshes the config, and the rail grows by one.
  const aiReady = !!config?.ai_review?.configured;
  const steps = useMemo(
    () =>
      WIZARD_STEPS.filter(
        (s) =>
          (s !== "analytics" || analyticsStep) && (s !== "contact" || aiReady),
      ),
    [analyticsStep, aiReady],
  );
  const afterAi: Step = aiReady ? "contact" : "project";
  // The endpoint was removed while the contact step was open (Back → AI → Remove key → Next is
  // not the only way): the step left the rail, so move on rather than render an unlisted step.
  if (step === "contact" && !aiReady) setStep("project");
  // Each step opens at its top (#1544): the card body is one scroller, and Next sits at the foot of a
  // long step (AI), so the next step would otherwise open scrolled past its own title.
  const paneRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const scroller = paneRef.current?.closest(`.${styles.cardBody}`);
    scroller?.scrollTo?.({ top: 0 });
  }, [step]);

  // Load engine discovery once we reach (or mount on) the wizard.
  useEffect(() => {
    if (mode !== "wizard") return;
    let alive = true;
    loadCatalog();
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
  }, [mode, loadCatalog]);

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

  // A launch in flight belongs to the wizard that is still open (#1739): leaving (Skip, Close, Esc,
  // Back, unmount) bumps the generation, and a create that answers afterwards opens nothing.
  const launchGen = useRef(0);
  useEffect(
    () => () => {
      launchGen.current += 1;
    },
    [],
  );
  const finishNow = useCallback(async () => {
    launchGen.current += 1;
    try {
      await api.completeOnboarding(newestRelease()?.version);
    } catch {
      /* non-fatal — the gate also infers; a retry happens on the next config load */
    }
    onClose();
  }, [onClose]);
  // An inline sign-in terminal is live in the Agents step: closing setup would close it and
  // interrupt the vendor's sign-in, so every way of closing asks first.
  const [signinLive, setSigninLive] = useState(false);
  const [askLeave, setAskLeave] = useState(false);
  const finish = useCallback(() => {
    if (signinLive) setAskLeave(true);
    else void finishNow();
  }, [signinLive, finishNow]);

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

  // An API client's server mints the id; a console client's id mode must be KNOWN before anything
  // is minted (#853 P4, #454).
  const launchReady =
    !!engine && roster.loaded && (launchApi || mintsOwnId(engine) !== undefined);
  const launchWait = !roster.loaded
    ? roster.status === "failed"
      ? "Couldn’t load the agent list — retrying. Launch becomes available when it loads."
      : "Loading the agent list…"
    : !engine
      ? "No agent can start a session on this host yet. Install one in the Agents step."
      : !launchApi && mintsOwnId(engine) === undefined
        ? "This agent isn’t in the loaded agent list, so a session can’t be started for it."
        : null;
  const launch = async () => {
    if (!engine || !effectiveCwd || !launchReady || busy) return;
    const gen = ++launchGen.current;
    setBusy(true);
    setLaunchErr(null);
    if (launchApi) {
      // Created FIRST, through New session's own create (#1739): a refused or lost create keeps the
      // wizard open with its reason and does not complete setup; a guarded retry reuses the same
      // operation id, so it never makes a second session.
      const skip = bypass;
      let key: string;
      try {
        // A skip create is started HERE, below, only while this launch is still wanted: a wizard
        // dismissed during the create must not leave an agent running unattended.
        key = await createStructuredSession({
          engine,
          cwd: effectiveCwd,
          bypass: skip,
          start: false,
        });
      } catch (e) {
        if (gen !== launchGen.current) return; // setup was left meanwhile
        setLaunchErr(
          (e instanceof ApiError && e.message ? e.message : "Couldn’t start that session.") +
            (skip && !(e instanceof ApiError && e.status < 500)
              ? " If it was created, it waits in your session list and runs only if you start it there."
              : ""),
        );
        setBusy(false);
        return;
      }
      // Setup was left while the create ran: the session exists (it waits in the session list),
      // but nothing completes or opens on behalf of a wizard the operator already dismissed.
      if (gen !== launchGen.current) return;
      try {
        await api.completeOnboarding(newestRelease()?.version);
      } catch {
        /* non-fatal */
      }
      if (gen !== launchGen.current) return; // …or while setup was being completed
      if (skip) startCreated(key);
      onClose();
      const id = key.slice(key.indexOf(":") + 1);
      openFullScreen(navigate, { seed: { key, engine, id, title: "New session" } });
      return;
    }
    try {
      await api.completeOnboarding(newestRelease()?.version);
    } catch {
      /* non-fatal */
    }
    if (gen !== launchGen.current) return; // setup was left while it was being completed
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
    <Overlay onClose={() => !askLeave && finish()} title="Set up BattleLab" wide>
      {askLeave && (
        <ConfirmDialog
          tag="SIGN-IN"
          title="Leave sign-in?"
          confirmLabel="Leave setup"
          cancelLabel="Stay"
          role="alertdialog"
          onCancel={() => setAskLeave(false)}
          onConfirm={() => {
            setAskLeave(false);
            void finishNow();
          }}
        >
          <p>
            Closing setup closes the temporary sign-in terminal and interrupts sign-in. You can
            start another attempt from Settings → Agents.
          </p>
        </ConfirmDialog>
      )}
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

      <div className={styles.pane} ref={paneRef}>
        {step === "welcome" && (
          <Step
            title="Welcome, Commander."
            desc="BattleLab is your command deck. A one-minute briefing, then the deck is yours."
          >
            <p className={styles.copy}>
              We'll check which agents report for duty on this host and connect
              your AI — a local model works. With AI connected, you pick your HQ
              contact: the General who briefs you. Then a first project, a short
              tour, and your first session.
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
            desc="This deck launches agents with permission bypass — guard it like SSH."
          >
            {loginOff ? (
              <>
                <div
                  className={styles.row}
                  style={{ alignItems: "flex-start" }}
                >
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

        {/* Kept mounted (hidden) once visited: an inline setup keeps its operation, its polling and
            any sign-in terminal across Next / Back (#1739). */}
        {agentsSeen && (
          <div hidden={step !== "agents"} data-testid="setup-agents-step">
          <Step
            title="Your agents"
            desc="BattleLab runs each agent through its API client by default: structured turns, approvals and background work, safe to leave running. Install the agents you use here; their terminal client stays available."
          >
            <SetupAgents
              roster={roster}
              catalog={catalog}
              catalogError={catalogErr}
              onChanged={loadCatalog}
              onSigninLive={setSigninLive}
            />
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
                onClick={() => setStep("system")}
              >
                Next <ArrowRight size={14} />
              </button>
            </Foot>
          </Step>
          </div>
        )}

        {step === "system" && (
          <SetupSystem
            onBack={() => setStep("agents")}
            onNext={() => setStep("ai")}
            onSkip={finish}
          />
        )}

        {step === "ai" && (
          <Step
            title="Set up your AI"
            desc="Your own AI — a local model or any OpenAI-compatible endpoint — powers HQ, session reviews, auto-sort and Missions (beta). Connect it, then pick the model."
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
                onClick={() => setStep("system")}
              >
                <ArrowLeft size={14} /> Back
              </button>
              <button
                type="button"
                className={`${styles.pri} shine`}
                onClick={() => setStep(afterAi)}
              >
                Next <ArrowRight size={14} />
              </button>
            </Foot>
          </Step>
        )}

        {step === "contact" && (
          <Step
            title="Choose your HQ contact"
            desc="The General who briefs you — HQ answers in their voice. Random brings a new face every day."
          >
            <HqContactGallery
              subheadClass="sr-only"
              hintClass={styles.copy}
            />
            <Foot>
              <button
                type="button"
                className={styles.ghost}
                onClick={() => setStep("project")}
              >
                Decide later
              </button>
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
                onClick={() => setStep(aiReady ? "contact" : "ai")}
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
            desc="A 30-second briefing on the main surfaces."
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
            desc="Everything's set, Commander — launch into your project."
          >
            <div
              className={styles.clients}
              role="radiogroup"
              aria-label="Agent"
              data-testid="setup-launch-agents"
            >
              {choices.api.length > 0 && (
                <span className={styles.clientGroup}>API · recommended</span>
              )}
              {choices.api.map((id) => (
                <ClientOption
                  key={id}
                  id={id}
                  checked={engine === id}
                  isDefault={id === choices.preselect}
                  sub={
                    engine === id && bypass
                      ? "Structured session · no approval prompts, no sandbox"
                      : "Structured session · approvals in the pane"
                  }
                  onPick={setEngineChoice}
                />
              ))}
              {/* An API client that cannot start is listed with its reason, never dropped (#1311). */}
              {choices.unready.map((c) => (
                <label
                  key={`unready:${c.id}`}
                  className={`${styles.client} ${styles.clientOff}`}
                  data-testid="setup-launch-unready"
                >
                  <input type="radio" name="setup-launch-agent" disabled />
                  <span className={styles.clientText}>
                    <span className={styles.clientName}>{c.label}</span>
                    <span className={styles.clientSub}>Not ready: {c.reason}</span>
                  </span>
                </label>
              ))}
              {choices.terminal.length > 0 && (
                <span className={styles.clientGroup}>Terminal</span>
              )}
              {choices.terminal.map((id) => (
                <ClientOption
                  key={id}
                  id={id}
                  checked={engine === id}
                  isDefault={id === choices.preselect}
                  sub="Runs in a terminal"
                  onPick={setEngineChoice}
                />
              ))}
            </div>
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
            {launchCanBypass && (
              <label className={styles.checkbox}>
                <input
                  type="checkbox"
                  checked={bypass}
                  onChange={(e) => setBypass(e.target.checked)}
                />
                <span>Skip permission prompts</span>
              </label>
            )}
            {/* The same disclosure as New session (#1339): for an API client, skipping prompts also
                drops its sandbox — said before Launch, whether the box was ticked here or by the
                stored default. */}
            {launchApi && bypass && (
              <p className={styles.warn} role="note" data-testid="setup-api-bypass-warning">
                {engineLabel(engineInfo(engine)?.api?.source ?? engine)} won’t ask before running
                commands or editing files, and runs without its sandbox. Fixed for this session.
              </p>
            )}
            <p className={styles.copy}>
              Want to read first? The{" "}
              <a href={DOCS_HOME_URL} target="_blank" rel="noopener noreferrer">
                docs
              </a>{" "}
              cover every feature — and the operator menu at the top right brings
              back the tour, the docs and What's new at any time.
            </p>
            {launchWait && (
              <p className={styles.note} role="status">
                {launchWait}
              </p>
            )}
            {launchErr && (
              <p className={styles.err} role="alert" data-testid="setup-launch-error">
                {launchErr}
              </p>
            )}
            <Foot>
              <button
                type="button"
                className={styles.ghost}
                onClick={finish}
                disabled={busy}
              >
                Finish without launching
              </button>
              <span className={styles.grow} />
              <button
                type="button"
                className={styles.btn}
                onClick={() => {
                  launchGen.current += 1;
                  setStep(analyticsStep ? "analytics" : "tour");
                }}
                disabled={busy}
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
        <header className={styles.bar}>
          <span className={styles.brand}>
            <BrandMark size={18} />
            {/* One flex item: the bar's gap belongs between the mark and the word, not inside it. */}
            <span>
              BATTLE<b>LAB</b>
            </span>
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

/** One startable client on the Launch step: a radio row named by the roster (#1739). */
function ClientOption({
  id,
  checked,
  isDefault,
  sub,
  onPick,
}: {
  id: string;
  checked: boolean;
  isDefault: boolean;
  sub: string;
  onPick: (id: string) => void;
}) {
  return (
    <label className={styles.client} data-engine={id}>
      <input
        type="radio"
        name="setup-launch-agent"
        value={id}
        checked={checked}
        onChange={() => onPick(id)}
      />
      <span className={styles.clientText}>
        <span className={styles.clientName}>
          {engineLabel(id)}
          {isDefault && <span className={styles.clientDefault}>default</span>}
        </span>
        <span className={styles.clientSub}>{sub}</span>
      </span>
    </label>
  );
}

const READ_WAIT_MS = 10_000;

/** "Your system" (#1819): what this host offers, the session limit picked for it, and why. Skipping
 *  setup, or pressing Next untouched, leaves the recommendation in force; only an edit is saved. */
function SetupSystem({
  onBack,
  onNext,
  onSkip,
}: {
  onBack: () => void;
  onNext: () => void;
  onSkip: () => void;
}) {
  const [settings, setSettings] = useState<ResourceSettings | null>(null);
  const [failed, setFailed] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api
      .resources()
      .then((r) => {
        if (!active) return;
        setSettings(r.settings);
        setDraft(String(r.settings.values.max_sessions));
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    // A read that never answers must not trap setup behind a disabled Next.
    const giveUp = setTimeout(() => setFailed(true), READ_WAIT_MS);
    return () => {
      active = false;
      clearTimeout(giveUp);
    };
  }, []);
  const bounds = settings?.bounds.max_sessions;
  const value = Number(draft);
  const valid =
    !!bounds && /^\d+$/.test(draft) && value >= bounds.min && value <= bounds.max;
  const edited = !!settings && valid && value !== settings.values.max_sessions;
  const next = async () => {
    if (!edited) {
      onNext();
      return;
    }
    setSaving(true);
    setError("");
    try {
      await api.setResources({ max_sessions: value });
      onNext();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The limit couldn't be saved.");
    } finally {
      setSaving(false);
    }
  };
  const recommended = settings?.recommended.max_sessions;
  return (
    <Step
      title="Sized for this machine."
      desc="We read this host and picked how many agent sessions can run at once."
    >
      {!settings && !failed && (
        <p className={styles.copy} role="status">
          Reading this system…
        </p>
      )}
      {failed && !settings && (
        <p className={styles.copy} role="alert">
          This system couldn't be read just now. BattleLab still picks a limit from it, and you can
          see and change that in Settings → Resources.
        </p>
      )}
      {settings && (
        <>
          <SessionCapacityFactsRow
            capacity={settings.capacity}
            label={settings.sources.max_sessions === "settings" ? "Recommended" : "Sessions"}
          />
          <p className={styles.copy} data-testid="setup-system-reason">
            {capacityReason(settings.capacity)}
          </p>
          <p className={styles.copy}>
            When {settings.values.max_sessions} are running, new sessions and missions are refused
            until one is archived or ends, or you raise the limit. Subagents inside a session don't
            count. Nothing running is ever stopped.
          </p>
          <label className={`${styles.field} ${styles.limitField}`}>
            <span>Maximum running sessions</span>
            <input
              type="number"
              inputMode="numeric"
              min={bounds?.min}
              max={bounds?.max}
              step="1"
              value={draft}
              disabled={saving}
              data-testid="setup-system-limit"
              onChange={(e) => {
                setDraft(e.target.value);
                setError("");
              }}
            />
          </label>
          <p className={styles.copy}>
            {settings.sources.max_sessions === "settings"
              ? `You set ${settings.values.max_sessions}. Recommended for this system: ${recommended}.`
              : `Recommended for this system: ${recommended}.`}{" "}
            Range: {bounds?.min}–{bounds?.max}. You can change this any time in Settings →
            Resources.
          </p>
        </>
      )}
      {error && (
        <p className={styles.copy} role="alert">
          {error}
        </p>
      )}
      <Foot>
        <button type="button" className={styles.ghost} onClick={onSkip}>
          Skip setup
        </button>
        <span className={styles.grow} />
        <button type="button" className={styles.btn} onClick={onBack}>
          <ArrowLeft size={14} /> Back
        </button>
        <button
          type="button"
          className={`${styles.pri} shine`}
          // Not past the explanation while it is still on its way; a failed read has its own words.
          disabled={saving || (settings ? !valid : !failed)}
          onClick={() => void next()}
        >
          Next <ArrowRight size={14} />
        </button>
      </Foot>
    </Step>
  );
}

function Foot({ children }: { children: React.ReactNode }) {
  return <div className={styles.footRow}>{children}</div>;
}
