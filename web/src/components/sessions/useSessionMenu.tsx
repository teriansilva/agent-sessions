import {
  Archive,
  ArchiveRestore,
  ArrowLeftRight,
  Bot,
  BotOff,
  Crosshair,
  Eye,
  EyeOff,
  FolderInput,
  Pencil,
  ScrollText,
  Sparkles,
  Star,
  Tag,
} from "lucide-react";
import { type ReactNode, useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import { isNewSessionPlaceholder } from "../../app/sessionsStore";
import {
  engineName,
  parseSessionKey,
  sessionPathFromKey,
} from "../../lib/format";
import { missionLink } from "../../lib/missionLink";
import type { ProjectRef, Session } from "../../types/api";
import type { RowMenuEntry } from "../sidebar/RowMenu";
import { MoveToProjectModal } from "../sidebar/MoveToProjectModal";
import rowStyles from "../sidebar/SessionList.module.css";
import { HandoffModal } from "../terminal/HandoffModal";
import { SessionRecapModal } from "../terminal/SessionRecapModal";
import { AdoptToMissionModal } from "./AdoptToMissionModal";
import { isAgent, useEngineRoster } from "../../app/engineRoster";

/** What a surface wires into the session menu. The sidebar backs these with its list's in-place
 *  row patches; the Overview map backs them with the same api calls plus a map refetch (#968). */
export interface SessionMenuHandlers {
  onToggleArchive: (id: string, currentlyArchived: boolean) => Promise<void>;
  /** Favorite toggle (#122): flips the row's `sticky` flag. */
  onToggleFavorite: (id: string, value: boolean) => Promise<void>;
  /** AI review (#356). Undefined while the feature is unconfigured — the items are hidden. */
  onReviewNow?: (id: string) => Promise<void>;
  onToggleReviewExcluded?: (id: string, excluded: boolean) => Promise<void>;
  onToggleOrchestratorExcluded?: (id: string, excluded: boolean) => Promise<void>;
  /** Reassign to a project entity, or `null` to unassign (#424 Phase 5b). */
  onSetProject: (id: string, ref: ProjectRef | null) => Promise<void>;
  /** Rename / Set tag. The EDITOR belongs to the surface: the sidebar edits inline in the row,
   *  the map opens a dialog, because a field inside React Flow's zoom transform is unreadable. */
  onEdit: (mode: "title" | "tag") => void;
}

/** A session's handoff peers (#597 Phase 2). A row can be the TARGET of one handoff and the SOURCE
 *  of another — a chained session carries BOTH, so both come back (Hermes on #703: `from || to`
 *  hid the outbound half of a chain). Shared by the menu's backlinks and the row's badges. */
export function sessionPeers(s: Session) {
  return (
    [
      { key: s.handoff_from ?? "", inbound: true },
      { key: s.handoff_to ?? "", inbound: false },
    ] as const
  )
    .filter((p) => p.key)
    .map((p) => ({
      ...p,
      parsed: parseSessionKey(p.key),
      path: sessionPathFromKey(p.key),
    }))
    .filter((p) => p.parsed !== null);
}

export interface SessionMenu {
  items: RowMenuEntry[];
  /** The dialogs the menu opens (brief, hand off, move, adopt). Render it wherever the surface
   *  keeps its overlays; each one portals or positions itself. */
  dialogs: ReactNode;
  /** True while one of those dialogs is open — a surface that unmounts its menu host when the
   *  menu closes must keep it while this holds, or the dialog would vanish with it. */
  dialogOpen: boolean;
  reviewing: boolean;
  busy: boolean;
  /** Run a mutation with the menu's items disabled; the sidebar's inline editor uses it too. */
  runBusy: (fn: () => Promise<void>) => Promise<void>;
}

/** The session ⋯ menu — ONE implementation for every surface that offers it (#968): the sidebar
 *  row (#384) and the Overview map's chips and windows. Two copies of this list is how the two
 *  surfaces would drift, so the items, their order, their accessible names, and the dialogs they
 *  open all live here.
 *
 *  Items are assembled as logical groups joined by `pushGroup`, which inserts a separator only
 *  *between* non-empty groups, so an absent group (no peers, AI-review not configured) never
 *  leaves a doubled or leading rule. Group order: primary session actions (mirrored from the
 *  terminal header) → mission → handoff provenance backlinks → AI-review → row management. Busy
 *  items stay in place (aria-disabled) rather than being removed, so the menu doesn't reflow. */
export function useSessionMenu(
  s: Session,
  h: SessionMenuHandlers,
  /** `reviewInFlight`: a review of this session is already running, started by a menu host that
   *  may no longer exist (#968 review). Review now stays disabled until it settles. */
  opts: { reviewInFlight?: boolean } = {},
): SessionMenu {
  // Re-render when the engine roster lands or changes (#853 P4): this renders agent names,
  // badges or colours, which come from the roster, not from a client-side list.
  useEngineRoster();
  const [busy, setBusy] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  // "Move to project" picker (#424 Phase 5b) — the keyboard path for drag-to-reassign.
  const [moving, setMoving] = useState(false);
  const [moveReturnFocus, setMoveReturnFocus] = useState<HTMLElement | null>(null);
  // "Adopt to mission" (#948 P5) — the same picker the pane header opens.
  const [adopting, setAdopting] = useState(false);
  const [adoptReturnFocus, setAdoptReturnFocus] = useState<HTMLElement | null>(null);
  // Session brief (Recap) + Hand off, mirrored from the terminal header (#597 follow-up).
  const [recapOpen, setRecapOpen] = useState(false);
  const [recapReturnFocus, setRecapReturnFocus] = useState<HTMLElement | null>(null);
  const [handoffOpen, setHandoffOpen] = useState(false);
  const [handoffReturnFocus, setHandoffReturnFocus] = useState<HTMLElement | null>(null);
  const navigate = useNavigate();

  const runBusy = useCallback(async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  }, []);

  const handleMove = async (ref: ProjectRef | null) => {
    setMoving(false);
    const current = s.project.kind === "project" ? s.project.id : null;
    const next = ref && ref.kind === "project" ? ref.id : null;
    if (next === current) return; // chose the current assignment → no-op
    await runBusy(() => h.onSetProject(s.id, ref));
  };

  const reviewNow = async () => {
    if (!h.onReviewNow) return;
    setReviewing(true);
    try {
      await h.onReviewNow(s.id);
    } catch {
      /* fail-soft (#356): the last good result + its stale age keep showing */
    } finally {
      setReviewing(false);
    }
  };

  const reviewBusy = reviewing || !!opts.reviewInFlight;

  const items: RowMenuEntry[] = [];
  const pushGroup = (group: RowMenuEntry[]) => {
    if (group.length === 0) return;
    if (items.length > 0) items.push("separator");
    items.push(...group);
  };

  // Primary actions, mirrored from the terminal header (#597 follow-up): the session brief and
  // Hand off. Handoff is offered for every engine except `shell` — the header's `canHandoff` gate
  // (no agent transcript to seed). Each stashes the focused element so focus returns on close.
  const primary: RowMenuEntry[] = [
    {
      key: "brief",
      label: "Session brief",
      ariaLabel: "Open session brief",
      icon: <ScrollText size={15} />,
      onSelect: () => {
        setRecapReturnFocus(document.activeElement as HTMLElement | null);
        setRecapOpen(true);
      },
    },
  ];
  // Only an engine with an agent behind it can be handed off (#853 P4 — the roster, not an id).
  if (isAgent(s.engine)) {
    primary.push({
      key: "handoff",
      label: "Hand off…",
      ariaLabel: "Hand off session to another engine",
      icon: <ArrowLeftRight size={15} />,
      onSelect: () => {
        setHandoffReturnFocus(document.activeElement as HTMLElement | null);
        setHandoffOpen(true);
      },
    });
  }
  pushGroup(primary);

  // Mission membership (#948 P5). `mission` ABSENT means the server could not read the store, so
  // neither item is offered: "Adopt" would advertise a mutation nobody checked, and "Open" would
  // name a mission nobody read. Archived rows and an unreconciled `new-<uuid>` placeholder (which
  // `canonical_key` refuses) are not adoptable either.
  if (s.mission !== undefined && !s.archived && !isNewSessionPlaceholder(s.id)) {
    const held = s.mission;
    pushGroup([
      held
        ? {
            key: "open-mission",
            label: "Open mission",
            ariaLabel: `Open mission ${held.title}`,
            icon: <Crosshair size={15} />,
            onSelect: () => navigate(missionLink(held.id)),
          }
        : {
            key: "adopt-mission",
            label: "Adopt to mission…",
            ariaLabel: "Adopt session to a mission",
            icon: <Crosshair size={15} />,
            disabled: busy,
            onSelect: () => {
              setAdoptReturnFocus(document.activeElement as HTMLElement | null);
              setAdopting(true);
            },
          },
    ]);
  }

  // Handoff provenance backlinks (#597 Phase 2): route to a peer session. Peer ids are display
  // strings — a peer may be archived/deleted, so this just navigates and lets that route render
  // its own empty state.
  const peerItems: RowMenuEntry[] = [];
  for (const p of sessionPeers(s)) {
    const path = p.path;
    if (!path || !p.parsed) continue;
    peerItems.push({
      key: p.inbound ? "handoff-source" : "handoff-target",
      label: p.inbound ? "Open source session" : "Open handoff target",
      ariaLabel: p.inbound
        ? `Open the session this was handed off from (${engineName(p.parsed.engine)})`
        : `Open the session this was handed off to (${engineName(p.parsed.engine)})`,
      icon: <ArrowLeftRight size={15} />,
      onSelect: () => navigate(path),
    });
  }
  pushGroup(peerItems);

  // AI-review actions — present only when the review handlers were passed down
  // (ai_review.configured); the exclude item flips its label for an excluded row.
  const reviewItems: RowMenuEntry[] = [];
  if (h.onReviewNow && !s.review_excluded) {
    reviewItems.push({
      key: "review",
      label: "Review now",
      ariaLabel: "Review session now",
      icon: <Sparkles size={15} className={reviewBusy ? rowStyles.spin : undefined} />,
      disabled: busy || reviewBusy,
      onSelect: () => void reviewNow(),
    });
  }
  const onToggleReviewExcluded = h.onToggleReviewExcluded;
  if (onToggleReviewExcluded) {
    reviewItems.push({
      key: "exclude",
      label: s.review_excluded ? "Include in AI review" : "Exclude from AI review",
      icon: s.review_excluded ? <Eye size={15} /> : <EyeOff size={15} />,
      disabled: busy || reviewBusy,
      onSelect: () =>
        void runBusy(() => onToggleReviewExcluded(s.id, !s.review_excluded)),
    });
  }
  const onToggleOrchestratorExcluded = h.onToggleOrchestratorExcluded;
  if (onToggleOrchestratorExcluded) {
    // Separate entry from "Exclude from AI review" on purpose: this withdraws only the
    // orchestrator's agency. The session keeps its summary and its needs-you flag.
    reviewItems.push({
      key: "orchestrate",
      label: s.orchestrator_excluded
        ? "Let mission control manage this"
        : "Stop mission control managing this",
      icon: s.orchestrator_excluded ? <Bot size={15} /> : <BotOff size={15} />,
      disabled: busy,
      onSelect: () =>
        void runBusy(() =>
          onToggleOrchestratorExcluded(s.id, !s.orchestrator_excluded),
        ),
    });
  }
  pushGroup(reviewItems);

  // Row management: favorite / rename / tag / move / archive.
  pushGroup([
    {
      // Favorite toggle (#508): the visible ★ lives as a small prefix on the row's meta line;
      // this item carries the on/off accessible state.
      key: "favorite",
      label: s.sticky ? "Unfavorite" : "Favorite",
      ariaLabel: s.sticky ? "Unfavorite session" : "Favorite session",
      icon: <Star size={15} fill={s.sticky ? "currentColor" : "none"} />,
      disabled: busy,
      onSelect: () => void runBusy(() => h.onToggleFavorite(s.id, !s.sticky)),
    },
    {
      key: "rename",
      label: "Rename",
      ariaLabel: "Rename session",
      icon: <Pencil size={15} />,
      disabled: busy,
      onSelect: () => h.onEdit("title"),
    },
    {
      // Custom tag (#551): the same editor as Rename, seeded with the current tag.
      key: "tag",
      label: s.tag ? "Edit tag…" : "Set tag…",
      ariaLabel: s.tag ? "Edit session tag" : "Set session tag",
      icon: <Tag size={15} />,
      disabled: busy,
      onSelect: () => h.onEdit("tag"),
    },
    {
      key: "move",
      label: "Move to project…",
      ariaLabel: "Move session to a project",
      icon: <FolderInput size={15} />,
      disabled: busy,
      onSelect: () => {
        // The trigger had focus when the menu item fired; restore to it when the modal closes.
        setMoveReturnFocus(document.activeElement as HTMLElement | null);
        setMoving(true);
      },
    },
    {
      key: "archive",
      label: s.archived ? "Unarchive" : "Archive",
      ariaLabel: s.archived ? "Unarchive session" : "Archive session",
      icon: s.archived ? <ArchiveRestore size={15} /> : <Archive size={15} />,
      disabled: busy,
      onSelect: () => void runBusy(() => h.onToggleArchive(s.id, s.archived)),
    },
  ]);

  const dialogs = (
    <>
      {adopting && (
        <AdoptToMissionModal
          session={s}
          sessionKey={s.id}
          onClose={() => setAdopting(false)}
          returnFocusTo={adoptReturnFocus}
        />
      )}
      {moving && (
        <MoveToProjectModal
          session={s}
          onCancel={() => setMoving(false)}
          onMove={(ref) => void handleMove(ref)}
          returnFocusTo={moveReturnFocus}
        />
      )}
      {/* The same modals the terminal header mounts, keyed to this session — no need to open it
          first (#597 follow-up). */}
      {recapOpen && (
        <SessionRecapModal
          sessionId={s.id}
          engine={s.engine}
          title={s.title}
          project={s.project}
          lastMtime={s.last_mtime}
          // The same resolver the row's own dot uses (#744) — one session, one status, whichever
          // surface you open the brief from.
          statusRow={s}
          summary={s.ai_summary}
          recap={s.ai_recap}
          interventionRequired={s.intervention_required}
          interventionReason={s.intervention_reason}
          reviewedAt={s.reviewed_at}
          reviewExcluded={s.review_excluded}
          onClose={() => setRecapOpen(false)}
          returnFocusTo={recapReturnFocus}
        />
      )}
      {handoffOpen && (
        <HandoffModal
          sessionId={s.id}
          engine={s.engine}
          title={s.title}
          onClose={() => setHandoffOpen(false)}
          returnFocusTo={handoffReturnFocus}
        />
      )}
    </>
  );

  return {
    items,
    dialogs,
    dialogOpen: adopting || moving || recapOpen || handoffOpen,
    reviewing: reviewBusy,
    busy,
    runBusy,
  };
}
