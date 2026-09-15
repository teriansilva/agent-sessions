import { MISSION_PATH } from "../lib/missionLink";

/** What's new (#971) — one entry per release, newest wins. The next release adds an entry here and
 *  its illustrations (`scripts/whatsnew-illustrations`); the dialog, the gate and the tour's
 *  "What's new" button need no edit. */

export interface WhatsNewCta {
  label: string;
  /** An in-app route: navigates, then counts as dismissed. */
  to?: string;
  /** An external page: opens in a new tab, then counts as dismissed. */
  href?: string;
}

export interface WhatsNewSlide {
  id: string;
  eyebrow: string;
  title: string;
  body: string;
  points?: string[];
  items?: { title: string; text: string }[];
  /** Intro tiles that jump to another slide by id. */
  tiles?: { label: string; text: string; slide: string }[];
  /** Base name under the release's `assetDir`; `<image>-still.svg` is the paused variant. */
  image?: string;
  cta?: WhatsNewCta;
  /** Label for this slide's primary button (default "Next", or "Let's go" on the last slide). */
  primary?: string;
}

export interface WhatsNewRelease {
  /** Exactly MAJOR.MINOR.PATCH — it is what gets stored as `whats_new_seen`. */
  version: string;
  /** Under `web/public`. */
  assetDir: string;
  slides: WhatsNewSlide[];
}

/** A page that exists before and after any launch; the release notes are linked from its top. */
export const DOCS_HOME_URL = "https://docs.battlelab.superstatus.io/";
export const TEMPLATES_ROUTE = "/templates";

export const RELEASES: readonly WhatsNewRelease[] = [
  {
    version: "0.20.0",
    assetDir: "whatsnew/0.20",
    slides: [
      {
        id: "intro",
        eyebrow: "Version 0.20",
        title: "The deck runs missions now.",
        body: "The biggest release since launch: mission control drives work to done, and every session gains a file manager, an editor and a template library.",
        tiles: [
          { label: "Mission control", text: "Say the outcome, it follows through", slide: "missions" },
          { label: "Files & git", text: "Upload in, commit and push out", slide: "files" },
          { label: "Edit in place", text: "A save that can't lose the agent's work", slide: "editing" },
          { label: "Templates", text: "Write it once, send it anywhere", slide: "templates" },
        ],
        primary: "Show me",
      },
      {
        id: "missions",
        eyebrow: "Mission control",
        title: "Say the outcome. It plans, dispatches and follows through.",
        body: "Describe what you want done. Mission control proposes a project, an agent and the objectives that define done — you review the plan, press Begin, and it starts the agent without a terminal.",
        points: [
          "Objectives settle on evidence — tests, checks, a live URL — not on a model's word",
          "When it is unsure, it asks you instead of guessing",
          "Already running a session? Adopt it from its header or ⋯ menu",
        ],
        image: "missions",
        cta: { label: "Open Missions", to: MISSION_PATH },
      },
      {
        id: "files",
        eyebrow: "Files & git",
        title: "Upload in, commit and push out.",
        body: "The Files panel in every session now writes. Drop in files or whole folders, and run git from its Git tab — fetch, pull, branches, stage, commit and push.",
        points: [
          "A name clash asks you: skip, keep both or replace",
          "Discarded changes stay recoverable by object id",
          "Every write refuses rather than forces",
        ],
        image: "files",
      },
      {
        id: "editing",
        eyebrow: "Edit in place",
        title: "Edit where you read. The agent's work stays safe.",
        body: "Open a file in the Files panel and press Edit — a full code editor on BattleLab's own colours, with Ctrl/Cmd+S to save.",
        points: [
          "If the agent changed the file meanwhile, the save stops and shows you both versions",
          "Overwrite unlocks only after you have looked at the copy on disk",
          "The replaced copy goes to a recovery store, not to nowhere",
        ],
        image: "editing",
      },
      {
        id: "templates",
        eyebrow: "Templates",
        title: "Write it once. Fill the blanks. Send it anywhere.",
        body: "A library of reusable instructions with fill-in fields, tags and images. Pick one from any composer — or save a good prompt as a template.",
        points: [
          "The preview is exactly what the agent receives",
          "Insert one into a mission brief, too",
        ],
        image: "templates",
        cta: { label: "Open Templates", to: TEMPLATES_ROUTE },
      },
      {
        id: "also",
        eyebrow: "Also new",
        title: "And a lot more around the deck.",
        body: "",
        items: [
          { title: "Agents & usage", text: "Each agent's plan or token budget, with an alert before it runs out" },
          { title: "Terminal text size & font", text: "Size is the agent's column count — pick it per device" },
          { title: "Windows on the map", text: "Open sessions as draggable windows on the Overview" },
          { title: "Settings, one page per section", text: "Plus an endpoint check before the key is saved" },
          { title: "Prompts", text: "Every system prompt the app sends, in one place" },
        ],
        cta: { label: "Open the docs", href: DOCS_HOME_URL },
      },
      {
        id: "moved",
        eyebrow: "What moved",
        title: "If you used 0.19, four things are somewhere new.",
        body: "",
        items: [
          { title: "Pulse is now Missions", text: "Old /pulse links redirect to /mission" },
          { title: "Scan depth", text: "Settings → AI → Mission control; the medium depth is gone" },
          { title: "AI key", text: "Bound to the endpoint it was saved for — a new host asks for it again" },
          { title: "Phone", text: "Session actions are one Actions menu; drawers close with a tap outside" },
        ],
        primary: "Let's go",
      },
    ],
  },
];
