/** ASK — `find` / `history` against `/api/pulse/ask/stream` (#878, streamed since #1171). Since
 * #1294 it lives in the right-hand sidebar (`AskSidebar`) that the corner icon opens on every route;
 * before that it was a page under Dashboard (#1058, #1171).
 *
 * It used to be the second mode of the mission composer, behind a `NEW MISSION | ASK` segmented
 * control on the mission landing. That put a question about *sessions* behind a section about
 * *missions*, and made it unlinkable: there was no URL that opened it. The transport, the payload,
 * the answer shape and the match rows are unchanged by that move — only where it lives.
 *
 * **The conversation is durable (#1443).** Each one is kept on the server (`/api/ask/*`), and a
 * turn is the SERVER's: it runs as a task there, so closing the panel, navigating, reloading or a
 * dropped connection never cancels it. A stream that ends early is a statement about the
 * connection, so the console reads the conversation again and, while that turn is still
 * `pending`, polls until it settles. The panel opens on an EMPTY thread whose conversation is
 * created on its first question; the head's switcher (`AskSwitcher`) opens a kept one, and New
 * conversation returns to an empty thread. Delete removes the conversation on screen, after a
 * confirm (the server refuses while it is still answering or a session from it is starting);
 * otherwise retention (30 days, 50 conversations) is the server's. `/api/pulse/ask[/stream]` stays the transient,
 * read-only seam for its other callers (#1397).
 *
 * Every late result is checked against the conversation it belongs to (`cidRef`), so an answer
 * for a conversation the operator has switched away from never lands in the one on screen. `live`
 * is hygiene — it keeps a late callback from setting state on a dead component — and it must be
 * set on the EFFECT RUN, not only cleared on cleanup: StrictMode runs an effect, cleans it up and
 * runs it again on the SAME fiber, and `useRef(true)` initialises once.
 *
 * **Missions are answers too (#1069).** The server may name missions beside sessions; they arrive as
 * `mission_matches` and render as their own group above the sessions, each opening the mission
 * through `missionLink` — the one deep link the console shape-checks.
 *
 * **A matched session opens two ways.** "Open" is the full-screen route, as before. "Open in map"
 * asks the map's window workspace for a floating window and goes to the map — the same request
 * the sidebar row and "To map" make (`requestOpen`, #936) — and an answer naming several sessions
 * offers "Open all in map". The drain hands a refused request back to the full-screen route, which
 * is right for one window and loses the tail of a batch, so a batch is held to what the map is
 * KNOWN to admit: no more windows than the cap still has room for (`room`, which counts a stored
 * layout not yet restored), and only once a map measurement said it can host at all (`hostable`
 * true — "never measured" is enough for one window, whose refusal has a home, not for several).
 * Window mode is offered only where it exists — never on a phone, never once the map has measured
 * too small to host one.
 *
 * **It is laid out like the mission thread, because it IS the same kind of surface (#1069).** A chat
 * column: a scrolling thread that grows UP from a composer docked on the bottom edge
 * (`.threadCol` / `.pane` / `.paneAtBottom` / `.composerDock`, the #942 layout), each question and
 * answer an `article` named for its speaker ("You" / "Answer", the mission thread's `MessageRow`
 * words). Before the first question the thread is empty, so a short greeting sits centred in it;
 * it gives way to the conversation, the way a chat does.
 *
 * **Its chrome is the switcher, a way to start over and a way to close (#1171, #1294, #1443).** The
 * head carries the conversation switcher, New conversation and the sidebar's ✕ — nothing else. NEEDS YOU still reaches the thread: an answer row for a session on that list
 * carries the marker and ⓘ.
 *
 * **The wait is shown (#1171).** The server streams the turn's steps — the model reading, the read
 * tool it is calling, which catalog a search covers and how many transcripts it checks — and the
 * pending turn names the step it is on under a moving scan bar. Text is revealed progressively —
 * instantly outside the Full motion mode (#1531).
 *
 * The composer box, the turn rows and the match rows are the console's own primitives
 * (`mission.module.css`) rather than a second copy of them — the same cross-directory import the
 * composer already made for the session pane's Send (`terminal/Compose.module.css`). One box, drawn
 * one way, wherever it appears.
 */
import { Info, Plus, Trash2, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";

import { Link, useNavigate } from "react-router-dom";

import { MAP_PATH, useMapWindows } from "../../app/workspaceWindows";
import { ACTION_RESOLVED_EVENT, announceActionResolved } from "../../lib/actionEvents";
import { ApiError, api } from "../../lib/api";
import { blockMarkup } from "../../lib/blockMarkup";
import { useIsMobile } from "../../lib/useIsMobile";
import { missionLink } from "../../lib/missionLink";
import { settingsPath } from "../../routes/settingsTabs";
import type {
  AskAttachment,
  AskConversationSummary,
  AskConversationTurn,
  AskFollow,
  AskOperationDecision,
  AskProposalCard,
  AskQuestionCard,
  AskSessionDecision,
  AskStoredMatch,
  AskStoredMissionMatch,
  AskTurnEvent,
  AskUpdate,
} from "../../types/api";

import styles from "../pulse/mission.module.css";
import {
  FollowingRow,
  NudgeCard,
  OperationCard,
  QuestionCard,
  SessionCard,
  UpdateRow,
} from "./AskCards";
import { AttachmentList } from "./AskAttachments";
import { ASK_SEEN_EVENT, placeUpdates } from "./askUpdates";
import { AskComposer } from "./AskComposer";
import { ASK_DRAWER_MQ, useAskPanel } from "./askPanel";
import { mapBatch, matchRoute, matchSeed } from "./askMatches";
import a from "./AskConsole.module.css";
import type { AskStep } from "./askStep";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import { AskSwitcher } from "./AskSwitcher";
import { AskWorking } from "./AskWorking";
import { contactById } from "../hq/contacts";
import { HqPortrait } from "../hq/HqPortrait";
import { useHqContact } from "../hq/useHqContact";
import { RevealText } from "./RevealText";
import { CallButton } from "../call/CallButton";
import { ASK_OPEN_CONVERSATION_EVENT, takeAskConversation } from "../call/callContext";

export interface AskTurn {
  /** The server's `turn_id`. */
  id: string;
  question: string;
  answer: string | null;
  error: string | null;
  /** The sessions the answer is ABOUT, each with the reason it matched. Without these an answer
   *  naming a session gives the operator no way to reach it. */
  matches: AskStoredMatch[];
  /** The missions the answer is about (#1069), each with why it matched. */
  missions: AskStoredMissionMatch[];
  /** The step the turn is on while it runs (#1171); `null` once it has settled. */
  step: AskStep | null;
  /** Still running on the server. */
  pending: boolean;
  /** Who answers it (#1525): a contact id, or "off" for plain HQ. */
  contact: string;
  /** Its answer arrived while this page watched — the only answer that writes itself out. One read
   *  back from the server is history and shows at once. */
  live?: boolean;
  /** The question this turn ended on, and what it proposed: nudges (#1443 Phase 2) and sessions
   *  to start (Phase 3a). */
  ask: AskQuestionCard | null;
  proposals: AskProposalCard[];
  /** When it was asked (server time), to place followed sessions' updates around it; `null` for
   *  a turn this page has not had back from the server yet. */
  at: number | null;
  /** Files shared with the question (#1707). */
  attachments: AskAttachment[];
}

/** How often a turn this page is not streaming is read again until it settles. */
const POLL_MS = 2000;
/** How often a conversation with an undecided or in-flight card is read again, so a decision
 *  taken elsewhere (Needs you, another tab) or a proposal's expiry reaches the card. */
export const CARD_POLL_MS = 15_000;
/** How long a conversation keeps reading after one of its cards was decided, even with nothing
 *  live or followed: the follow that decision earns is written by the server's next follower pass,
 *  up to a minute later, and must still reach an open conversation (#1443 Phase 4). */
export const RECONCILE_MS = 180_000;
/** Projections that can still change on their own — including `unknown`, an unreadable ledger
 *  that recovers without anyone acting. */
const LIVE_PROJECTIONS = new Set([
  "actionable",
  "in_flight_revocable",
  "in_flight_locked",
  "unknown",
]);
/** A nudge's ledger outcomes that earn its conversation a follow (`ask_follow.NUDGE_FOLLOWS`). */
const NUDGE_FOLLOWS = new Set(["delivered", "indeterminate"]);
/** A session card's states that can still change: undecided, launching, or unreadable. */
const LIVE_SESSION_STATES = new Set(["pending", "deciding", "unknown"]);

function cardLive(p: AskProposalCard): boolean {
  if (p.kind === "session" || p.kind === "operation") return LIVE_SESSION_STATES.has(p.state);
  return LIVE_PROJECTIONS.has(p.projection);
}

function fromServer(t: AskConversationTurn): AskTurn {
  return {
    id: t.turn_id,
    question: t.question,
    answer: t.answer,
    error:
      t.status === "failed"
        ? t.detail || "That didn't work."
        : null,
    matches: t.matches ?? [],
    missions: t.mission_matches ?? [],
    step: null,
    pending: t.status === "pending",
    ask: t.ask ?? null,
    proposals: t.proposals ?? [],
    at: typeof t.asked_at === "number" ? t.asked_at : null,
    contact: t.contact ?? "off",
    attachments: t.attachments ?? [],
  };
}

/** An answer's speaker (#1525): the contact stored on ITS turn, so switching relabels only new
 *  answers. Off, a removed contact and a turn from before HQ read as plain HQ. */
/** A staged file's DELETE, retried until it lands — never silently given up (#1720 round 5).
 *
 *  The server's `discard` correctly declines (409) while a turn holds the conversation, and a
 *  turn can outlive any fixed attempt window. A capped retry that then returned void left the
 *  blob staged invisibly (quota stranded until the 24 h reap). So: keep trying while the page
 *  lives — fast at first, then slow — and count the file as PENDING CLEANUP the whole time so
 *  the composer can say so and the next upload knows the slot is not actually free.
 */
export type DiscardState = { pending: number };

async function discardWithRetry(
  cid: string,
  aid: string,
  onPending: (delta: number) => void,
): Promise<void> {
  onPending(1);
  try {
    for (let i = 0; i < 1000; i += 1) {
      try {
        await api.askDiscardAttachment(cid, aid);
        return;
      } catch (e) {
        // 409 is the server's last word (gone or already sent) — stop. Everything else is
        // RETRYABLE (#1720 round 11): 503 means the durable marker could not be written and
        // the file still counts, so the cleanup must stay pending and keep trying.
        if (e instanceof ApiError && e.status === 409) return;
        await new Promise((r) => setTimeout(r, i < 8 ? 2500 : 15000));
      }
    }
  } finally {
    onPending(-1);
  }
}

function speakerName(id: string): string {
  return contactById(id)?.name ?? "HQ";
}

function AnswerHead({ contact }: { contact: string }) {
  const who = contactById(contact);
  return (
    <div className={`${styles.eventHead} ${a.label} ${a.speaker}`} data-testid="ask-speaker">
      {who ? (
        <img
          className={a.speakerPoster}
          src={who.poster}
          alt=""
          aria-hidden="true"
          width={24}
          height={32}
          draggable={false}
        />
      ) : null}
      {who ? who.name : "HQ"}
    </div>
  );
}

/** Fold one streamed progress line into its turn. */
function applyEvent(t: AskTurn, ev: AskTurnEvent): AskTurn {
  if (ev.type !== "progress") return t;
  if (ev.step === "catalog")
    return { ...t, step: { step: "catalog", sessions: ev.sessions, missions: ev.missions } };
  if (ev.step === "content") return { ...t, step: { step: "content", candidates: ev.candidates } };
  if (ev.step === "tool") return { ...t, step: { step: "tool", tool: ev.tool } };
  return { ...t, step: { step: "thinking", round: ev.round } };
}

/** The server's turns, plus any turn this page submitted that the server does not have yet (its
 *  conversation is still being created, or the read was taken before it was stored). */
function merge(server: AskTurn[], local: AskTurn[]): AskTurn[] {
  const known = new Set(server.map((t) => t.id));
  return [...server, ...local.filter((t) => !known.has(t.id) && t.pending && !t.error)];
}

interface Gate {
  promise: Promise<void>;
  settled: boolean;
  open: () => void;
}

/** A one-shot gate: `promise` resolves, and `settled` turns true, on the first `open()`. */
function gate(): Gate {
  let resolve!: () => void;
  const g: Gate = {
    promise: new Promise<void>((r) => (resolve = r)),
    settled: false,
    open: () => {
      g.settled = true;
      resolve();
    },
  };
  return g;
}

function newTurnId(): string {
  return crypto.randomUUID();
}

export function AskConsole({
  configured,
  needsYou,
  onDetails,
  onClose,
  closeRef,
  cardPollMs = CARD_POLL_MS,
  visible = true,
  reconcileMs = RECONCILE_MS,
}: {
  /** False when no AI endpoint is configured. Ask has no local fallback, so the control is
   *  disabled and says why. */
  configured: boolean;
  /** Sessions currently on the NEEDS YOU list: an answer row for one of them carries the marker
   *  and ⓘ, which opens the same details the list does (#1086). */
  needsYou?: Set<string>;
  onDetails?: (sessionId: string) => void;
  /** The sidebar's ✕ (#1294). */
  onClose?: () => void;
  /** The ✕ itself: the mobile drawer moves focus to it on open, as the bell's drawer does. */
  closeRef?: RefObject<HTMLButtonElement | null>;
  /** How often undecided cards are re-read; a test shortens it rather than faking the clock. */
  cardPollMs?: number;
  /** Whether the operator can see this console (the sidebar is open): only then is an update
   *  marked seen (#1443 Phase 4). */
  visible?: boolean;
  /** How long the read continues after a session starts; a test shortens it. */
  reconcileMs?: number;
}) {
  const [turns, setTurns] = useState<AskTurn[]>([]);
  // #1443 Phase 4: followed sessions' updates, which sessions are followed, and unseen updates.
  const [updates, setUpdates] = useState<AskUpdate[]>([]);
  const [following, setFollowing] = useState<string[]>([]);
  const [follows, setFollows] = useState<AskFollow[]>([]);
  const [followsUnknown, setFollowsUnknown] = useState(false);
  // The ONE resolver the header, the dashboard card and a turn's `contact` share (#1525).
  const { contact, wire: contactWire } = useHqContact();
  // Read at send time: a send callback made before a switch must still say who answers now.
  const contactRef = useRef(contactWire);
  useEffect(() => {
    contactRef.current = contactWire;
  }, [contactWire]);
  const [updatesPending, setUpdatesPending] = useState(false);
  const [followsExhausted, setFollowsExhausted] = useState(false);
  const [unread, setUnread] = useState(false);
  // A session just started or a nudge just delivered: keep reading for a while, so the follow it
  // earns arrives.
  const startedSeen = useRef(new Map<string, string>());
  const [reconciling, setReconciling] = useState(false);
  const reconcileTimer = useRef<number | null>(null);
  const reconcile = useCallback(() => {
    setReconciling(true);
    if (reconcileTimer.current !== null) window.clearTimeout(reconcileTimer.current);
    reconcileTimer.current = window.setTimeout(() => {
      reconcileTimer.current = null;
      setReconciling(false);
    }, reconcileMs);
  }, [reconcileMs]);
  useEffect(
    () => () => {
      if (reconcileTimer.current !== null) window.clearTimeout(reconcileTimer.current);
    },
    [],
  );
  const [cid, setCid] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [conversations, setConversations] = useState<AskConversationSummary[] | null>(null);
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const map = useMapWindows();
  // Window mode where it exists: the map provider is mounted, this is not a phone, and the map
  // has not measured too small to host one (`null` = never measured, treated as available — the
  // drain's hand-back covers a wrong guess, as for the pane's "To map").
  const canMap = Boolean(map) && !isMobile && map?.hostable !== false;
  const openInMap = useCallback(
    (list: AskStoredMatch[]) => {
      if (!map || !list.length) return;
      for (const m of list) map.requestOpen(matchSeed(m));
      navigate(MAP_PATH);
    },
    [map, navigate],
  );
  const [streaming, setStreaming] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  /** False once this console has unmounted (module note). */
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  /** Every read of a conversation, and every send, takes a new generation; a read applies only if
   *  no newer one started since. So a slow read never overwrites a turn submitted after it was
   *  sent, and A→B→A never lets the first A read land over the second. */
  const gen = useRef(0);
  /** The first selection (reopen the latest conversation, or none). A question asked before it
   *  settles is shown at once and WAITS for it, then goes to the conversation it opened (or a new
   *  one) — the latest conversation opening is never allowed to sweep the question away. */
  const selectedRef = useRef<Gate | null>(null);
  if (selectedRef.current === null) selectedRef.current = gate();
  /** The conversation whose read last landed. A send into one whose read was fenced off re-reads
   *  it after settling, so its earlier turns are shown. */
  const loadedFor = useRef<string | null>(null);
  /** The conversation on screen — read at RESOLUTION time, never captured. */
  const cidRef = useRef<string | null>(null);
  /** One in-flight conversation creation shared by every staged file AND the first question
   *  (#1707). #1720 round 3: bound to a conversation GENERATION — a switch/New invalidates the
   *  flight at once, and a creation that resolves late can no longer install its id into the new
   *  screen. Round 5: `ask` shares this flight too, so a first Send and a first Attach can never
   *  create two different conversations. */
  const ensureCid = useRef<Promise<string> | null>(null);
  const convGen = useRef(0);
  /** Files whose DELETE has not landed yet (#1720 round 5): the composer says so, and the
   *  quota they still hold is never silently promised as free. */
  const [cleanupPending, setCleanupPending] = useState(0);
  // A call started from HQ's phone drawer (#1640 review 6907): the drawer closes first, so the
  // call panel owns focus instead of the drawer's trap, and the call hands focus back to HQ's
  // corner icon (the drawer's own button is hidden by then).
  const askPanel = useAskPanel();
  const leaveDrawerForCall = () => {
    if (!(window.matchMedia?.(ASK_DRAWER_MQ).matches ?? false)) return undefined;
    askPanel.close();
    return askPanel.triggerRef.current;
  };
  const current = (id: string | null) => live.current && cidRef.current === id;

  /** The stream being watched. Aborting it stops WATCHING; the server's turn goes on. */
  const inflight = useRef<AbortController | null>(null);
  useEffect(
    () => () =>
      queueMicrotask(() => {
        if (!live.current) inflight.current?.abort();
      }),
    [],
  );
  const stopWatching = () => {
    inflight.current?.abort();
    inflight.current = null;
    setStreaming(false);
  };

  /** Every read of the conversation LIST takes a generation; only the newest one applies. A read
   *  sent before a delete must never put the deleted conversation back (Hermes on #1564). */
  const listGen = useRef(0);
  const refreshList = useCallback(async () => {
    const g = ++listGen.current;
    try {
      const r = await api.askConversations();
      if (live.current && g === listGen.current) setConversations(r.conversations ?? []);
    } catch {
      /* the switcher keeps what it had */
    }
  }, []);

  /** Show conversation `id` as the server has it. */
  const load = useCallback(async (id: string) => {
    // A read for a conversation that is not on screen would only advance the generation and
    // fence off the read the screen IS waiting for — and its answer would be dropped anyway.
    if (!current(id)) return;
    const g = ++gen.current;
    const fresh = () => current(id) && g === gen.current;
    try {
      const c = await api.askConversation(id);
      if (!fresh()) return;
      loadedFor.current = id;
      ensureCid.current = Promise.resolve(id); // uploads go to the conversation on screen
      setTitle(c.title ?? "");
      setTurns((prev) => merge((c.turns ?? []).map(fromServer), prev));
      setUpdates(c.updates ?? []);
      // An unreadable follow state is not "following nothing" (Hermes on #1500): keep what was
      // last verified, and keep reading (below) until a read succeeds.
      setFollowsUnknown(Boolean(c.follows_incomplete));
      setUpdatesPending(Boolean(c.updates_pending));
      setFollowsExhausted(Boolean(c.follows_exhausted));
      if (!c.follows_incomplete) {
        setFollowing(c.following ?? []);
        setFollows(c.follows ?? (c.following ?? []).map((key) => ({ key, title: "" })));
        setUnread(Boolean(c.unread));
      }
      // A session STARTED, or a nudge DELIVERED (#1517) — from this card or another surface's —
      // earns a follow at the server's next follower pass, up to a minute later. Seen in that
      // state and not followed yet, the conversation keeps reading for a while (once per card's
      // move into it), so the follow, or the refusal row, reaches the open screen.
      const was = startedSeen.current;
      let earned = false;
      for (const t of c.turns ?? [])
        for (const p of t.proposals ?? []) {
          const state = p.state ?? "";
          const before = was.get(p.action_id);
          was.set(p.action_id, state);
          // An HQ operation (#1559) earns no follow: it changes something, it starts nothing.
          if (p.kind === "operation") continue;
          const key = p.kind === "session" ? p.session_key : p.key;
          const earns = p.kind === "session" ? state === "started" : NUDGE_FOLLOWS.has(state);
          if (earns && before !== state && key && !(c.following ?? []).includes(key))
            earned = true;
        }
      if (earned) reconcile();
    } catch {
      // Keep what is shown; a fresh array re-arms the poll of a still-pending turn.
      if (fresh()) setTurns((prev) => [...prev]);
    }
    // `current` reads refs only; `reconcile` is stable.
  }, [reconcile]);

  /** `initial`: the first selection, which may land under a question already asked — so it
   *  neither stops that question's watch nor clears it from the thread. */
  const openConversation = useCallback(
    (id: string, initial = false) => {
      if (!initial) stopWatching();
      convGen.current += 1; // in-flight uploads belong to the conversation they started on
      ensureCid.current = null;
      cidRef.current = id;
      setCid(id);
      setTitle("");
      setTurns((prev) => (initial ? prev.filter((t) => t.pending && !t.error) : []));
      setUpdates([]);
      setFollowing([]);
      setFollows([]);
      setFollowsUnknown(false);
      setUpdatesPending(false);
      setFollowsExhausted(false);
      setUnread(false);
      startedSeen.current = new Map();
      void load(id);
    },
    [load],
  );
  // A call's "Continue in text" / "Type" (#1397): HQ opens on the call's conversation, also when
  // the request came before this console mounted (the first open of HQ).
  useEffect(() => {
    const open = (id: string | null) => {
      if (typeof id === "string" && id && id !== cidRef.current) openConversation(id);
    };
    open(takeAskConversation());
    const on = () => open(takeAskConversation());
    window.addEventListener(ASK_OPEN_CONVERSATION_EVENT, on);
    return () => window.removeEventListener(ASK_OPEN_CONVERSATION_EVENT, on);
  }, [openConversation]);

  /** An empty thread: nothing on screen and no conversation chosen; the first question creates
   *  one. What New conversation does, and where a deleted conversation leaves the operator. */
  const startNew = useCallback(() => {
    stopWatching();
    // The flight must die with the conversation it belonged to (#1720 round 4): without these
    // two lines the next upload awaits the OLD conversation's cached promise and uploads there.
    convGen.current += 1;
    ensureCid.current = null;
    cidRef.current = null;
    setCid(null);
    setTitle("");
    setTurns([]);
    setUpdates([]);
    setFollowing([]);
    setFollows([]);
    setFollowsUnknown(false);
    setUpdatesPending(false);
    setFollowsExhausted(false);
    setUnread(false);
    startedSeen.current = new Map();
    inputRef.current?.focus();
  }, []);

  /** Deleting the conversation on screen: confirmed first, then the server deletes it with
   *  everything kept for it. A refusal (HQ still answering, a session still starting) is said in
   *  the dialog and nothing changes. */
  /** The button that opened the confirm (focus returns to it); `null` = no confirm open. */
  const [confirmDelete, setConfirmDelete] = useState<HTMLElement | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const deleteCurrent = useCallback(async () => {
    const id = cidRef.current;
    if (!id) {
      setConfirmDelete(null);
      return;
    }
    setDeleting(true);
    setDeleteError(null);
    try {
      await api.askDeleteConversation(id);
      if (!live.current) return;
      setConfirmDelete(null);
      listGen.current++; // a list read still in flight predates the delete: it lands nowhere
      setConversations((rows) => (rows ? rows.filter((c) => c.id !== id) : rows));
      if (cidRef.current === id) startNew();
      void refreshList();
    } catch (e) {
      if (live.current)
        setDeleteError(e instanceof Error && e.message ? e.message : "It could not be deleted.");
    } finally {
      if (live.current) setDeleting(false);
    }
  }, [refreshList, startNew]);

  // On first mount: the switcher's list. HQ opens on an EMPTY conversation; the kept ones are a
  // pick in the switcher away, and none is reopened on its own.
  useEffect(() => {
    let cancelled = false;
    const g = ++listGen.current;
    const newest = () => !cancelled && live.current && g === listGen.current;
    void (async () => {
      try {
        const r = await api.askConversations();
        if (newest()) setConversations(r.conversations ?? []);
      } catch {
        if (newest()) setConversations([]);
      } finally {
        if (!cancelled) selectedRef.current?.open();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // A turn still running on the server that this page is not streaming (a reload, a dropped
  // connection, another tab) is read again until it settles.
  const waiting = !streaming && turns.some((t) => t.pending);
  useEffect(() => {
    if (!waiting || !cid) return;
    const timer = window.setTimeout(() => void load(cid), POLL_MS);
    return () => window.clearTimeout(timer);
  }, [waiting, cid, turns, load]);

  const busy = streaming || turns.some((t) => t.pending);

  // A card offers Send / Dismiss only while the LEDGER does. A decision taken elsewhere (Needs
  // you, the bell, another tab) or the proposal's TTL can settle it under an open card, so while
  // any card is undecided or in flight the conversation is read again: on the app's own
  // action-resolved event at once, and on a timer for everything that event cannot see.
  // …and while sessions are followed: their updates land between turns (#1443 Phase 4).
  const cardsLive =
    turns.some((t) => t.proposals.some(cardLive)) ||
    following.length > 0 ||
    reconciling ||
    // Keep reading while the follow state is unknown or an update is still being delivered:
    // either would otherwise strand an update off the open screen (Hermes on #1500).
    followsUnknown ||
    updatesPending;
  // A session being started settles in seconds, not minutes: follow it at the turn poll's pace.
  // A session starting, or an operation running (#1559), settles in seconds: follow it closely.
  const launching = turns.some((t) =>
    t.proposals.some(
      (p) => (p.kind === "session" || p.kind === "operation") && p.state === "deciding",
    ),
  );
  const every = launching ? Math.min(POLL_MS, cardPollMs) : cardPollMs;
  useEffect(() => {
    if (!cardsLive || streaming || !cid) return;
    const timer = window.setInterval(() => void load(cid), every);
    const onResolved = () => void load(cid);
    window.addEventListener(ACTION_RESOLVED_EVENT, onResolved);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener(ACTION_RESOLVED_EVENT, onResolved);
    };
  }, [cardsLive, streaming, cid, load, every]);

  const ask = useCallback(
    async (
      q: string,
      atts: AskAttachment[] = [],
      reply?: { answers: string; option?: number },
    ): Promise<{
      outcome: "sent" | "refused" | "canceled" | "uncertain";
      conv: string | null;
    }> => {
      // The aim is CAPTURED (#1720 round 12): the thread this message was aimed at — created
      // or not — never whatever `cidRef` says at settlement time. A switch/New that aborts the
      // watch is "canceled", and a canceled draft may never be restored into the new thread.
      let aimConv: string | null = null;
      const wrap = (outcome: "sent" | "refused" | "canceled" | "uncertain") => ({
        outcome,
        conv: aimConv,
      });
      const refuse = () => wrap("refused");
      const sent = () => wrap("sent");
      const canceled = () => wrap("canceled");
      const uncertain = () => wrap("uncertain");
      // A canceled WATCH does not cancel the server turn (#1720 round 13): reconcile the
      // original turn id before calling the draft unsent. Found → it sent.
      //
      // Round 14: ABSENCE IS ONLY PROOF WHEN NO POST WAS EVER MADE. After the server has
      // received the streamed POST but before it appends the user turn, a reconcile GET sees
      // nothing — and the turn then lands anyway. So a missing turn is indeterminate for as
      // long as the POST may still be in flight: the draft is kept UNCERTAIN (never a blind
      // resend) unless the message request never started at all.
      const reconcileOr = async (
        fallback: () => { outcome: "sent" | "refused" | "canceled" | "uncertain"; conv: string | null },
      ) => {
        if (id !== null) {
          try {
            const c = await api.askConversation(id);
            if ((c.turns ?? []).some((t) => t.turn_id === turnId)) return sent();
          } catch {
            return uncertain();
          }
        }
        // The POST may still be admitted behind us: absence is not proof it was refused.
        return messageStarted ? uncertain() : fallback();
      };
      if ((!q && !atts.length) || busy || !configured) return refuse();
      const turnId = newTurnId();
      gen.current++; // a read already in flight must not land over this turn
      const ctl = new AbortController();
      inflight.current = ctl;
      setStreaming(true);
      const optimistic: AskTurn = {
        id: turnId,
        question: q,
        answer: null,
        error: null,
        matches: [],
        missions: [],
        step: null,
        pending: true,
        ask: null,
        proposals: [],
        at: null,
        contact: contactRef.current,
        attachments: atts,
      };
      setTurns((prev) => [...prev, optimistic]);
      const update = (fn: (t: AskTurn) => AskTurn) =>
        setTurns((prev) => prev.map((t) => (t.id === turnId ? fn(t) : t)));
      let id: string | null = null;
      let messageStarted = false; // (#1720 r11) a failure BEFORE the message is a refusal
      try {
        const sel = selectedRef.current;
        if (sel && !sel.settled) {
          await sel.promise;
          if (!live.current || ctl.signal.aborted) return reconcileOr(canceled);
        }
        // The SAME single-flight creation the file path uses (#1720 round 5): a first Send and
        // a first Attach racing each other must never create two conversations and split the
        // message from its files. The generation fence keeps a late settlement off a new screen.
        const gen = convGen.current;
        if (!ensureCid.current) {
          ensureCid.current = (async () => {
            let id = cidRef.current;
            if (id === null) {
              const c = await api.askNewConversation();
              if (!c?.id) throw new Error("Could not start a conversation — ask again.");
              id = c.id;
              if (gen === convGen.current) {
                cidRef.current = id;
                loadedFor.current = id; // created empty: nothing earlier to read
                setCid(id);
              }
            }
            return id;
          })().catch((e) => {
            if (gen === convGen.current) ensureCid.current = null;
            throw e;
          });
        }
        id = await ensureCid.current;
        aimConv = id;
        if (!live.current || ctl.signal.aborted) return reconcileOr(canceled);
        messageStarted = true;
        const settled = await api.askMessageStream(
          id,
          {
            turnId,
            // An option is sent by INDEX: the server answers with the reply it stored.
            text: reply?.option !== undefined ? "" : q,
            ...(reply ? { answers: reply.answers } : {}),
            ...(reply?.option !== undefined ? { option: reply.option } : {}),
            contact: optimistic.contact,
            ...(atts.length ? { attachments: atts.map((x) => x.id) } : {}),
          },
          (ev) => {
            if (current(id)) update((t) => applyEvent(t, ev));
          },
          ctl.signal,
        );
        // A watch this page stopped (New conversation, a switch) applies nothing, however
        // late — and the draft it held is reconciled first (#1720 round 13): an admitted turn
        // must never be offered as "Not sent" and resent.
        if (ctl.signal.aborted) return reconcileOr(canceled);
        if (current(id)) {
          const done: AskTurn = { ...fromServer(settled), live: true };
          // Upsert: whatever a read did meanwhile, the settled turn is shown. A turn that answers
          // a question marks that question answered — as the server derives it on the next read.
          // Only a SETTLED answer answers — a failed one leaves the question open to try again.
          const answered = settled.status === "done" ? (settled.answers ?? null) : null;
          setTurns((prev) =>
            (prev.some((t) => t.id === turnId)
              ? prev.map((t) => (t.id === turnId ? done : t))
              : [...prev, done]
            ).map((t) =>
              answered && t.ask?.question_id === answered && !t.ask.answered
                ? { ...t, ask: { ...t.ask, answered: true } }
                : t,
            ),
          );
          if (!title) setTitle(settled.question);
          if (loadedFor.current !== id) void load(id);
        }
        void refreshList();
        return sent();
      } catch (err) {
        if (!live.current || ctl.signal.aborted) return reconcileOr(canceled);
        const msg = err instanceof Error ? err.message : "That didn't work.";
        if (id !== null && current(id)) {
          // The turn may be stored and still running — ask the server what became of it.
          const g = ++gen.current;
          try {
            const c = await api.askConversation(id);
            if (!current(id) || g !== gen.current) return refuse();
            if ((c.turns ?? []).some((t) => t.turn_id === turnId)) {
              setTurns((prev) => merge(c.turns.map(fromServer), prev));
              return sent(); // it landed after all — uncertain resolved toward the server
            }
          } catch {
            /* fall through: say what failed */
          }
        }
        if (current(id)) update((t) => ({ ...t, error: msg, step: null, pending: false }));
        // #1720 round 5: only a CONFIRMED server refusal (nothing was written) may restore the
        // draft. Round 11: a failure BEFORE the message request ever started (a failed
        // conversation creation, an abort in between) is just as confirmed. Anything uncertain
        // after the message started — a dropped stream after acceptance — is "sent": the turn
        // may be the server's, and a restored draft would duplicate it.
        const confirmed =
          !messageStarted ||
          (err instanceof ApiError &&
            (err.status === 409 || err.status === 422 || err.status === 503));
        return confirmed ? refuse() : sent();
      } finally {
        if (inflight.current === ctl) {
          inflight.current = null;
          setStreaming(false);
        }
      }
    },
    // `current` reads refs only.
    [busy, configured, load, refreshList, title],
  );

  /** A nudge card's decision goes to the SAME routes every decision uses; the card then reads
   *  the ledger's own verdict back (a 409 "out of date" is an outcome, not an error). */
  const [deciding, setDeciding] = useState<ReadonlySet<string>>(new Set());
  const decide = useCallback(
    async (actionId: string, approve: boolean) => {
      const id = cidRef.current;
      setDeciding((s) => new Set(s).add(actionId));
      try {
        await (approve ? api.approveAction(actionId) : api.rejectAction(actionId));
        // Everything else showing this decision (the bell, Needs you) hears it at once.
        announceActionResolved(null);
      } catch {
        /* the re-read below shows what the ledger says happened */
      } finally {
        setDeciding((s) => {
          const n = new Set(s);
          n.delete(actionId);
          return n;
        });
        // Only the conversation still on screen is re-read: a decision that settles after the
        // operator switched away must not disturb the one they are now reading.
        if (id && cidRef.current === id) void load(id);
      }
    },
    [load],
  );

  /** A session card's tap (#1443 Phase 3a): the decision and the bypass value the card showed —
   *  never what to start. 202 = launching; the re-read follows it. A refusal that changed the
   *  card (the default moved) shows on the card; one that did not is said under it. */
  const [cardErrors, setCardErrors] = useState<ReadonlyMap<string, string>>(new Map());
  const decideSession = useCallback(
    async (pid: string, decision: "start" | "dismiss", bypass: boolean) => {
      const id = cidRef.current;
      setDeciding((s) => new Set(s).add(pid));
      let error: string | null = null;
      // The server's answer about THIS card — applied at once, so an accepted Start never stays
      // clickable (nor a changed default unseen) if the re-read below fails; the re-read and the
      // poll reconcile it.
      let decided: AskSessionDecision | null = null;
      try {
        decided = (await api.decideAskProposal(pid, decision, bypass)).proposal;
      } catch (e) {
        const carried =
          e instanceof ApiError
            ? ((e.record as { proposal?: AskSessionDecision } | undefined)?.proposal ?? null)
            : null;
        if (carried) decided = carried;
        else error = e instanceof Error && e.message ? e.message : "That did not go through.";
      } finally {
        if (decided && id && cidRef.current === id) {
          const view = decided;
          gen.current++; // a read already in flight must not land over this answer
          setTurns((prev) =>
            prev.map((t) => ({
              ...t,
              proposals: t.proposals.map((p) =>
                p.kind === "session" && p.action_id === pid ? { ...p, ...view } : p,
              ),
            })),
          );
        }
        setDeciding((s) => {
          const n = new Set(s);
          n.delete(pid);
          return n;
        });
        setCardErrors((m) => {
          const n = new Map(m);
          if (error) n.set(pid, error);
          else n.delete(pid);
          return n;
        });
        if (id && cidRef.current === id) {
          reconcile();
          void load(id);
        }
      }
    },
    [load, reconcile],
  );

  /** An operation card's tap (#1559): the decision and what the card asked the operator for (a
   *  picked folder) — never what to run. 202 = running; the re-read and the poll follow it. A
   *  refusal that did not change the card (a 422 for a missing folder) is said under it. */
  const decideOperation = useCallback(
    async (pid: string, decision: "confirm" | "dismiss", inputs?: { folder: string }) => {
      const id = cidRef.current;
      setDeciding((s) => new Set(s).add(pid));
      let error: string | null = null;
      let decided: AskOperationDecision | null = null;
      try {
        decided = (await api.decideAskOperation(pid, decision, inputs)).proposal;
      } catch (e) {
        const carried =
          e instanceof ApiError
            ? ((e.record as { proposal?: AskOperationDecision } | undefined)?.proposal ?? null)
            : null;
        if (carried) decided = carried;
        else error = e instanceof Error && e.message ? e.message : "That did not go through.";
      } finally {
        if (decided && id && cidRef.current === id) {
          const view = decided;
          gen.current++; // a read already in flight must not land over this answer
          setTurns((prev) =>
            prev.map((t) => ({
              ...t,
              proposals: t.proposals.map((p) =>
                p.kind === "operation" && p.action_id === pid ? { ...p, ...view } : p,
              ),
            })),
          );
        }
        setDeciding((s) => {
          const n = new Set(s);
          n.delete(pid);
          return n;
        });
        setCardErrors((m) => {
          const n = new Map(m);
          if (error) n.set(pid, error);
          else n.delete(pid);
          return n;
        });
        if (id && cidRef.current === id) void load(id);
      }
    },
    [load],
  );

  /** #1443 Phase 4: an update is SEEN once this conversation is on screen with it — the sidebar
   *  open and the tab visible. Everything else showing the unread state (the Ask icon's dot, the
   *  switcher) hears it at once. */
  // Only what is ON SCREEN is seen: the newest update this page has rendered, never "now" — an
  // update written after the last read stays unread until it is shown.
  const shownThrough = updates.reduce((m, u) => Math.max(m, u.ts), 0);
  // Only the NEWEST mark's answer applies: an older one resolving late never restores "unread".
  const seenGen = useRef(0);
  useEffect(() => {
    if (!unread || !cid || !visible || shownThrough <= 0) return;
    const id = cid;
    const mark = () => {
      if (document.visibilityState !== "visible") return;
      const g = ++seenGen.current;
      void api
        .askSeen(id, shownThrough)
        .then((r) => {
          if (g !== seenGen.current) return;
          if (cidRef.current === id) setUnread(Boolean(r.unread));
          window.dispatchEvent(new CustomEvent(ASK_SEEN_EVENT));
        })
        .catch(() => {
          /* still unread; the next read or visibility change tries again */
        });
    };
    mark();
    document.addEventListener("visibilitychange", mark);
    return () => document.removeEventListener("visibilitychange", mark);
  }, [unread, cid, visible, shownThrough]);

  const [unfollowing, setUnfollowing] = useState<ReadonlySet<string>>(new Set());
  const unfollow = useCallback(
    async (key: string) => {
      const id = cidRef.current;
      if (!id) return;
      setUnfollowing((s) => new Set(s).add(key));
      try {
        await api.askUnfollow(id, key);
        if (cidRef.current === id) {
          setFollowing((f) => f.filter((k) => k !== key));
          setFollows((f) => f.filter((x) => x.key !== key));
          setUpdates((us) => us.map((u) => (u.key === key ? { ...u, following: false } : u)));
        }
      } catch {
        /* the re-read below shows whether it is still followed */
      } finally {
        setUnfollowing((s) => {
          const n = new Set(s);
          n.delete(key);
          return n;
        });
        if (cidRef.current === id) void load(id);
      }
    },
    [load],
  );

  const placed = useMemo(() => placeUpdates(turns, updates), [turns, updates]);
  const updateRow = (u: AskUpdate) => (
    <UpdateRow
      key={`${u.key}:${u.ts}:${u.kind}`}
      u={u}
      busy={unfollowing.has(u.key)}
      onUnfollow={() => void unfollow(u.key)}
      open={
        <Link
          className={styles.openSession}
          to={matchRoute(u.key)}
          aria-label={`Open ${u.title || "the session"}`}
          data-testid="ask-update-open"
        >
          Open
        </Link>
      }
    />
  );

  /** The newest turn is where the operator is looking: keep the thread scrolled to it, as a chat
   *  does — when a turn is added, the conversation changes, or the operator was already at the
   *  bottom. A BACKGROUND re-read (cards, polling) never pulls someone reading earlier turns away. */
  const paneRef = useRef<HTMLDivElement | null>(null);
  const atBottom = useRef(true);
  const shown = useRef<{ cid: string | null; count: number }>({ cid: null, count: 0 });
  useLayoutEffect(() => {
    const el = paneRef.current;
    const moved = shown.current.cid !== cid || shown.current.count !== turns.length;
    shown.current = { cid, count: turns.length };
    if (el && turns.length && (moved || atBottom.current)) el.scrollTop = el.scrollHeight;
  }, [turns, cid]);

  return (
    <div className={`${styles.threadCol} ${a.col}`} data-testid="ask-col">
      <header
        className={`${a.measure} ${a.head} ${contact ? a.headContact : ""}`}
        data-testid="ask-head"
        data-contact={contact ? contact.id : "off"}
      >
        {/* #1525: [portrait] [HQ + the contact's name / picker · + · ×]. Off keeps one row. */}
        {contact ? (
          // The scan frame (#1531): decorative, Full only, paused with the ambient pause.
          <span className={`${a.portraitFrame} nj-ambient`} aria-hidden="true">
            <HqPortrait
              contact={contact}
              playing={visible}
              className={a.headPortrait}
              testId="hq-head-portrait"
            />
          </span>
        ) : null}
        <div className={a.headMain}>
        <div className={a.who}>
          <h2 className={a.title}>HQ</h2>
          {contact ? (
            <span className={a.whoName} data-testid="hq-head-name">
              {contact.name}
            </span>
          ) : null}
        </div>
        <div className={a.headTools}>
        <AskSwitcher
          conversations={conversations}
          currentId={cid}
          currentTitle={title || (turns[0]?.question ?? "")}
          onOpen={() => void refreshList()}
          onPick={(id) => {
            if (id !== cidRef.current) openConversation(id);
          }}
        />
        {/* Calling HQ (#1397): a call from here continues THIS conversation by voice. */}
        <CallButton variant="icon" testId="ask-call" conversationId={cid} beforeStart={leaveDrawerForCall} />
        <button
          type="button"
          className={a.newBtn}
          aria-label="Delete this conversation"
          disabled={cid === null}
          onClick={(e) => {
            setDeleteError(null);
            setConfirmDelete(e.currentTarget);
          }}
          data-testid="ask-delete"
          title="Delete this conversation"
        >
          <Trash2 size={17} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={a.newBtn}
          aria-label="New conversation"
          disabled={turns.length === 0}
          onClick={startNew}
          data-testid="ask-new"
          title="New conversation"
        >
          <Plus size={18} aria-hidden="true" />
        </button>
        {onClose ? (
          <button
            ref={closeRef}
            type="button"
            className={a.back}
            aria-label="Close HQ"
            onClick={onClose}
            data-testid="ask-close"
          >
            <X size={18} aria-hidden="true" />
          </button>
        ) : null}
        </div>
        </div>
      </header>
      {confirmDelete ? (
        <ConfirmDialog
          tag="DELETE CONVERSATION"
          title={`Delete “${title || turns[0]?.question || "this conversation"}”?`}
          confirmLabel={deleting ? "Deleting…" : "Delete"}
          danger
          busy={deleting}
          onCancel={() => setConfirmDelete(null)}
          onConfirm={() => void deleteCurrent()}
          returnFocusTo={confirmDelete}
        >
          <p>
            Its questions, answers and cards are removed, and it stops following its sessions.
            Sessions it started and messages already sent are not touched. This can't be undone.
          </p>
          {deleteError ? (
            <p role="alert" data-testid="ask-delete-error">
              {deleteError}
            </p>
          ) : null}
        </ConfirmDialog>
      ) : null}
      <div
        className={styles.pane}
        ref={paneRef}
        data-testid="ask-pane"
        onScroll={(e) => {
          const el = e.currentTarget;
          atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight <= 40;
        }}
      >
        {turns.length === 0 ? (
          <div className={`${a.measure} ${a.intro}`} data-testid="ask-empty">
            <p className={a.greeting}>
              Ask about anything your sessions and missions hold — answers are
              read from the transcripts this install can already see.
            </p>
            {!configured ? (
              <p className={a.needsEndpoint} data-testid="ask-needs-endpoint">
                Ask needs an AI endpoint — it has no local fallback. Set one up
                in{" "}
                <Link to={settingsPath("ai-endpoint")}>
                  Settings → Endpoint &amp; model
                </Link>
                , then come back.
              </p>
            ) : null}
          </div>
        ) : (
          <div
            className={`${a.measure} ${styles.paneAtBottom}`}
            data-testid="ask-turns"
          >
            {placed.before.map(updateRow)}
            {turns.map((t) => (
              <div key={t.id} data-testid="ask-turn">
                <article
                  className={`${styles.event} ${a.you}`}
                  aria-label="You"
                >
                  <div className={`${styles.eventHead} ${a.label}`}>You</div>
                  <div className={`${styles.eventText} ${a.text}`}>{t.question}</div>
                  <AttachmentList
                    cid={cid ?? null}
                    attachments={t.attachments}
                    expanded={t.pending || t.live === true}
                  />
                </article>
                <article
                  className={`${styles.event} ${a.answer}`}
                  aria-label={`Answer from ${speakerName(t.contact)}`}
                  aria-busy={t.pending}
                  data-speaker={t.contact}
                >
                  <AnswerHead contact={t.contact} />
                  {t.answer !== null ? (
                    <>
                      <RevealText
                        className={`${styles.eventText} ${a.text} ${a.md}`}
                        text={t.answer}
                        testId="ask-answer"
                        instant={!t.live}
                        streaming={t.pending}
                        markup={blockMarkup}
                      />
                      {/* Missions first, then sessions — each group labelled only when there
                          are missions, so a sessions-only answer reads as it did before #1069. */}
                      {t.missions.length > 0 ? (
                        <>
                          <div className={`${styles.eventHead} ${a.label} ${a.group}`}>
                            Missions
                          </div>
                          {t.missions.map((m) => (
                            <div
                              key={m.id}
                              className={`${styles.matchRow} ${a.arrive}`}
                              data-testid="ask-mission-match"
                            >
                              <div className={styles.matchBody}>
                                <div className={`${styles.eventText} ${a.text}`}>{m.title}</div>
                                {m.why ? (
                                  <div className={`${styles.objReason} ${a.reason}`}>{m.why}</div>
                                ) : null}
                              </div>
                              <Link
                                className={styles.openSession}
                                to={missionLink(m.id)}
                                aria-label={`Open mission ${m.title}`}
                              >
                                Open mission
                              </Link>
                            </div>
                          ))}
                          {t.matches.length > 0 ? (
                            <div className={`${styles.eventHead} ${a.label} ${a.group}`}>
                              Sessions
                            </div>
                          ) : null}
                        </>
                      ) : null}
                      {/* The matched sessions, each with why it matched and a way in. An answer
                          that names a session the operator cannot reach is half an answer. */}
                      {t.matches.map((m) => (
                        <div
                          key={m.id}
                          className={`${styles.matchRow} ${a.arrive}`}
                          data-testid="ask-match"
                        >
                          <div
                            className={`${styles.matchBody}${canMap ? ` ${a.matchBody}` : ""}`}
                          >
                            <div className={`${styles.eventText} ${a.text}`}>
                              {m.title}
                              {needsYou?.has(m.id) ? (
                                <span className={a.needsTag} data-testid="ask-match-needs-you">
                                  <span className={a.needsDot} aria-hidden="true" />
                                  Needs you
                                </span>
                              ) : null}
                            </div>
                            {m.why ? (
                              <div className={`${styles.objReason} ${a.reason}`}>{m.why}</div>
                            ) : null}
                          </div>
                          {needsYou?.has(m.id) && onDetails ? (
                            <button
                              type="button"
                              className={a.detailsBtn}
                              aria-label={`Details for ${m.title}`}
                              onClick={() => onDetails(m.id)}
                            >
                              <Info size={16} aria-hidden="true" />
                            </button>
                          ) : null}
                          <span className={a.opens} data-testid="ask-match-opens">
                            <Link
                              className={styles.openSession}
                              to={matchRoute(m.id)}
                              aria-label={`Open ${m.title}`}
                              data-testid="ask-match-open"
                            >
                              Open
                            </Link>
                            {canMap ? (
                              <button
                                type="button"
                                className={styles.openSession}
                                aria-label={`Open ${m.title} in map`}
                                title={
                                  map?.openKeys.has(m.id)
                                    ? "Already open on the map — show its window"
                                    : map && map.room < 1
                                      ? "The map's window limit is reached — close a window or raise the limit"
                                      : "Open as a floating window on the map"
                                }
                                disabled={
                                  !map?.openKeys.has(m.id) && (map?.room ?? 0) < 1
                                }
                                onClick={() => openInMap([m])}
                                data-testid="ask-match-map"
                              >
                                Open in map
                              </button>
                            ) : null}
                          </span>
                        </div>
                      ))}
                      {canMap && map && map.hostable === true && t.matches.length > 1
                        ? (() => {
                            const { take, left } = mapBatch(
                              t.matches,
                              map.openKeys,
                              map.room,
                            );
                            return (
                              <div className={a.batch}>
                                <button
                                  type="button"
                                  className={styles.openSession}
                                  disabled={take.length === 0}
                                  onClick={() => openInMap(take)}
                                  title={
                                    left > 0
                                      ? `The map's window limit leaves room for ${take.length} of these ${t.matches.length} — close a window or raise the limit for the rest`
                                      : `Open all ${t.matches.length} as floating windows on the map`
                                  }
                                  data-testid="ask-match-map-all"
                                >
                                  {left > 0
                                    ? `Open ${take.length} of ${t.matches.length} in map`
                                    : `Open all ${t.matches.length} in map`}
                                </button>
                              </div>
                            );
                          })()
                        : null}
                    </>
                  ) : t.pending ? (
                    <AskWorking step={t.step} />
                  ) : null}
                  {t.error ? (
                    <div className={`${styles.objStale} ${a.errorRow}`} data-testid="ask-error">
                      {t.error}
                    </div>
                  ) : null}
                  {/* Cards are the server's records, not part of the answer text: a turn that
                      staged a proposal and then failed still shows the live decision. */}
                  {t.ask ? (
                    <QuestionCard
                      ask={t.ask}
                      disabled={busy || !configured}
                      onAnswer={(r) =>
                        "option" in r
                          ? void ask(t.ask!.options[r.option], [], {
                              answers: t.ask!.question_id,
                              option: r.option,
                            })
                          : void ask(r.text, [], { answers: t.ask!.question_id })
                      }
                    />
                  ) : null}
                  {t.proposals.map((p) =>
                    p.kind === "session" ? (
                      <SessionCard
                        // A new starting value (the card asks again) starts the control afresh.
                        key={`${p.action_id}:${p.bypass_default}`}
                        p={p}
                        deciding={deciding.has(p.action_id)}
                        error={cardErrors.get(p.action_id) ?? null}
                        onStart={(bypass) => void decideSession(p.action_id, "start", bypass)}
                        onDismiss={() => void decideSession(p.action_id, "dismiss", false)}
                        ways={
                          p.session_key ? (
                            <>
                              <Link
                                className={styles.openSession}
                                to={matchRoute(p.session_key)}
                                aria-label={`Open the session started in ${p.title}`}
                                data-testid="ask-session-open"
                              >
                                Open
                              </Link>
                              {canMap ? (
                                <button
                                  type="button"
                                  className={styles.openSession}
                                  aria-label={`Open the session started in ${p.title} in map`}
                                  disabled={
                                    !map?.openKeys.has(p.session_key) && (map?.room ?? 0) < 1
                                  }
                                  onClick={() =>
                                    openInMap([{ id: p.session_key, title: p.title, why: "" }])
                                  }
                                  data-testid="ask-session-map"
                                >
                                  Open in map
                                </button>
                              ) : null}
                            </>
                          ) : null
                        }
                      />
                    ) : p.kind === "operation" ? (
                      <OperationCard
                        key={p.action_id}
                        p={p}
                        deciding={deciding.has(p.action_id)}
                        error={cardErrors.get(p.action_id) ?? null}
                        onConfirm={(inputs) => void decideOperation(p.action_id, "confirm", inputs)}
                        onDismiss={() => void decideOperation(p.action_id, "dismiss")}
                      />
                    ) : (
                      <NudgeCard
                        key={p.action_id}
                        p={p}
                        deciding={deciding.has(p.action_id)}
                        onApprove={() => void decide(p.action_id, true)}
                        onDismiss={() => void decide(p.action_id, false)}
                      />
                    ),
                  )}
                </article>
                {(placed.after.get(t.id) ?? []).map(updateRow)}
              </div>
            ))}
            {follows.length > 0 ? (
              <section
                className={a.following}
                aria-label="Sessions this conversation follows"
                data-testid="ask-following"
              >
                {follows.map((f) => (
                  <FollowingRow
                    key={f.key}
                    title={f.title}
                    busy={unfollowing.has(f.key)}
                    onUnfollow={() => void unfollow(f.key)}
                    open={
                      <Link
                        className={styles.openSession}
                        to={matchRoute(f.key)}
                        aria-label={`Open ${f.title || "the session"}`}
                        data-testid="ask-following-open"
                      >
                        Open
                      </Link>
                    }
                  />
                ))}
              </section>
            ) : null}
            {followsExhausted ? (
              <p className={a.followNotice} role="note" data-testid="ask-follow-exhausted">
                This conversation no longer follows sessions. Start a new conversation to follow
                more.
              </p>
            ) : null}
          </div>
        )}
      </div>
      {/* THE COMPOSER, DOCKED — the mission thread's #942 dock, on the bottom edge at every
          height, under the same centred measure as the thread. */}
      <div className={styles.composerDock}>
        <div className={a.measure}>
          <AskComposer
            configured={configured}
            busy={busy}
            onAsk={(q, atts) => ask(q, atts)} // the outcome drives the composer's draft restore
        convKey={cid}
        uploadFile={async (file) => {
          // The conversation is created by what the operator does in it — the first question or
          // the first file (#1707), never by New conversation. SINGLE-FLIGHT: several files
          // picked at once must not each create one (the last id would win and the rest would be
          // claimed against the wrong conversation).
          const gen = convGen.current;
          if (!ensureCid.current) {
            ensureCid.current = (async () => {
              let id = cidRef.current;
              if (id === null) {
                const c = await api.askNewConversation();
                if (!c?.id) throw new Error("Could not start a conversation — try again.");
                id = c.id;
                // A creation that settles after a switch/New must not install its id into the
                // new screen (#1720 round 3): it returns its id to ITS uploader and stays gone.
                if (gen === convGen.current) {
                  cidRef.current = id;
                  loadedFor.current = id;
                  setCid(id);
                }
              }
              return id;
            })().catch((e) => {
              // A failed creation is never cached (#1720 review): the next file retries.
              if (gen === convGen.current) ensureCid.current = null;
              throw e;
            });
          }
          const id = await ensureCid.current;
          const { attachment } = await api.askUploadAttachment(id, file);
          return { attachment, cid: id };
        }}
        discardFile={(cid, attachment) => {
          // Discarded against the CID the file was UPLOADED to, never whatever is on screen now
          // (#1720 round 3): after A→B the id means nothing in B, and after New there is no B.
          // Retried until it lands, with a visible pending count while it has not (#1720 round 5).
          void discardWithRetry(cid, attachment.id, (d) =>
            setCleanupPending((n) => Math.max(0, n + d)),
          );
        }}
        cleanupPending={cleanupPending}
            inputRef={inputRef}
          />
        </div>
      </div>
    </div>
  );
}
