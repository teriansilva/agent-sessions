/** The ONE executor of HQ's screen directives (#2102). A plain class, so its rules — apply once,
 *  admit against the map's room in the order asked, hold on a guarded page, never lose the ones
 *  queued behind a navigation — are pinned without a browser. `ScreenProvider` mounts it above the
 *  router, beside the workspace, and hands it the router and the workspace through `ScreenDeps`;
 *  the typed stream and the call socket both feed `execute`.
 *
 *  What it never does: run a directive read back from a conversation (only a live event reaches
 *  `execute`), report an outcome to the server (the outcome is this device's own), or open
 *  anything but an app route the table in `screenDirective.ts` names.
 *
 *  **How it decides, and how it learns what happened.** The workspace it can read is the last
 *  RENDER's, and that lags its own dispatches: several directives arrive between two renders.
 *  So it never decides from that snapshot alone:
 *
 *  - **A shadow of the workspace.** Every action it dispatches is numbered (`seq`) and kept
 *    until the committed state says it has applied it (`WorkspaceState.seq`). The shadow is the
 *    committed state with the actions still outstanding replayed through the workspace's OWN
 *    reducer, in dispatch order. It is recomputed from the committed state every time, so a
 *    change the executor did not make (the Commander closing a window by hand) is simply there.
 *  - **Admission reads where the shadow will SETTLE** (`settledWorkspace`: the stored layout
 *    restored, the queue replayed): is there room, does the session have a window, is anything
 *    on screen.
 *  - **Order is the workspace's.** A command asked for behind a queued open queues behind it,
 *    and the drain replays the queue in order (`QueuedCommand`). The executor adds no ordering
 *    of its own, so a batch delivered before a render ends where the same directives, applied
 *    one at a time with a drain between each, would end.
 *  - **Every command is acknowledged by id.** Each carries its directive's id; the workspace
 *    answers that id with what it did (`WorkspaceAck`: applied, a no-op and why, refused and
 *    why, cancelled). An outcome is only ever written from its own acknowledgement — never
 *    from a counter, never from a later request's — and one that is not answered in time is
 *    withdrawn and says so. */
import { matchRoute, matchSeed } from "../components/ask/askMatches";
import {
  settledWorkspace,
  workspaceReducer,
  type WorkspaceAck,
  type WorkspaceCommand,
  type WorkspaceState,
} from "../components/overview/useWorkspace";
import { MAP_PATH } from "../lib/routes";
import {
  directiveRoute,
  parseDirective,
  sessionOf,
  type MapRefusal,
  type ScreenDirective,
  type ScreenOpen,
  type ScreenOutcome,
} from "./screenDirective";

/** Who delivered a directive: the HQ console's stream, or the call's socket. */
export type ScreenSource = "typed" | "call";

/** What the executor needs from the app. Every member reads the CURRENT state when called. */
export interface ScreenDeps {
  /** Go to an app route. `held`: an unsaved-changes guard asked and the Commander stayed. */
  navigate(to: string): Promise<"applied" | "held">;
  /** Is the map the route on screen? */
  onMap(): boolean;
  /** Could a map host a window on this device (not a phone, not measured too small)? */
  mapCapable(): boolean;
  /** The workspace as last rendered: the authority, as far as it has got. */
  workspace(): WorkspaceState;
  /** One numbered action for the workspace. */
  dispatch(action: WorkspaceCommand): void;
  /** A directive put something on screen: the phone's HQ drawer closes, the call minimises on a
   *  phone, and focus moves into what opened (`window`: that session's map window). */
  shown(source: ScreenSource, window: string | null): void;
}

export interface StoredOutcome {
  outcome: ScreenOutcome;
  title: string;
}
export type Outcomes = ReadonlyMap<string, StoredOutcome>;

const CONSUMED_KEY = "hq-screen-consumed";
const CONSUMED_MAX = 200;
/** How long a command waits for the map to answer it. The map can fail to mount (its sessions
 *  never load); a command that was never run is then withdrawn, and the line says so. */
export const MAP_ANSWER_MS = 20_000;

/** The ids already handled, kept for the tab's life so a reload followed by a replayed event
 *  still does nothing. Best effort: without storage the in-memory set is the whole fence. */
function loadConsumed(): string[] {
  try {
    const raw = JSON.parse(sessionStorage.getItem(CONSUMED_KEY) ?? "[]") as unknown;
    return Array.isArray(raw) ? raw.filter((v): v is string => typeof v === "string") : [];
  } catch {
    return [];
  }
}

/** A command sent to the workspace and not answered yet. */
interface Waiting {
  d: ScreenDirective;
  turn: string;
  source: ScreenSource;
  timer: ReturnType<typeof setTimeout>;
}

/** One directive of a turn that changed the map, in the order they were applied: what a later
 *  one on the same session supersedes. */
interface Claim {
  id: string;
  /** The session it is about; `null` for a command about every window. */
  key: string | null;
  arrange: boolean;
}

const holds = (ws: WorkspaceState, key: string) =>
  ws.windows.some((w) => w.key === key || w.actionKey === key);

export class ScreenExecutor {
  private deps: ScreenDeps;
  private readonly consumed: Set<string>;
  private outcomes: Map<string, StoredOutcome> = new Map();
  private readonly listeners = new Set<() => void>();
  /** Directives are applied one after another: a navigation a guard is holding keeps the ones
   *  behind it waiting, in order, instead of racing past it. */
  private chain: Promise<void> = Promise.resolve();
  /** This executor's own actions the committed workspace has not applied yet, in order. */
  private outstanding: (WorkspaceCommand & { seq: number })[] = [];
  private seq = 0;
  /** Commands sent and not acknowledged yet, by directive id (which is the command's id). */
  private readonly waiting = new Map<string, Waiting>();
  /** Whoever waits for the map to have answered everything (`answers`). */
  private readonly quiet: (() => void)[] = [];
  /** Per turn: windows admitted so far, whether one request already fell back to full screen
   *  (the rest of a batch is then listed, never opened one over the other), and the directives
   *  that changed the map so far. */
  private readonly turns = new Map<
    string,
    { admitted: number; fellBack: boolean; claims: Claim[] }
  >();

  constructor(deps: ScreenDeps) {
    this.deps = deps;
    this.consumed = new Set(loadConsumed());
    this.seq = deps.workspace().seq;
  }

  /** The app as it is NOW. The provider rebinds on every render, so each decision reads the
   *  current workspace and viewport, however long ago the executor was made. */
  bind(deps: ScreenDeps): void {
    this.deps = deps;
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  snapshot = (): Outcomes => this.outcomes;

  private set(id: string, outcome: ScreenOutcome, title?: string): void {
    const next = new Map(this.outcomes);
    next.set(id, { outcome, title: title ?? this.outcomes.get(id)?.title ?? "" });
    this.outcomes = next;
    for (const fn of this.listeners) fn();
  }

  private consume(id: string): boolean {
    if (this.consumed.has(id)) return false;
    this.consumed.add(id);
    try {
      sessionStorage.setItem(
        CONSUMED_KEY,
        JSON.stringify([...this.consumed].slice(-CONSUMED_MAX)),
      );
    } catch {
      /* no storage: the in-memory set still holds for this page */
    }
    return true;
  }

  /** A LIVE directive from the stream or the call socket. Handled at most once per id, whatever
   *  became of it the first time: a duplicate event, a reconnect's replay and a repeat after a
   *  cancelled navigation all do nothing. Returns whether this call took it. */
  execute(
    raw: unknown,
    meta: { title?: string; turnId?: string | null; source: ScreenSource },
  ): boolean {
    const d = parseDirective(raw);
    // An unknown member is ignored: its record reads as a link-less note.
    if (!d || !this.consume(d.id)) return false;
    const title = (meta.title ?? "").slice(0, 200);
    this.set(d.id, { state: "pending" }, title);
    const turn = meta.turnId || d.id;
    this.chain = this.chain
      .then(() => this.apply(d, title, turn, meta.source))
      .catch(() => undefined);
    return true;
  }

  /** Resolves once every directive taken so far has been applied (tests). */
  idle(): Promise<void> {
    return this.chain;
  }

  // ---- the workspace: the shadow, and what it will settle to ----------------------------------

  /** The committed workspace with this executor's outstanding actions replayed, in order. */
  private shadow(): WorkspaceState {
    const real = this.deps.workspace();
    this.outstanding = this.outstanding.filter((a) => a.seq > real.seq);
    return this.outstanding.reduce(workspaceReducer, real);
  }

  /** Where the workspace will be once the map has restored its layout and drained its queue. */
  private settled(): WorkspaceState {
    return settledWorkspace(this.shadow());
  }

  /** Send one numbered command: it joins the shadow now and the workspace at its next render. */
  private send(action: WorkspaceCommand): void {
    this.seq = Math.max(this.seq, this.deps.workspace().seq) + 1;
    const numbered = { ...action, seq: this.seq };
    this.outstanding.push(numbered);
    this.deps.dispatch(numbered);
  }

  /** Send a command that answers to `d.id`, and wait for that answer — but not for ever. */
  private ask(d: ScreenDirective, turn: string, source: ScreenSource, action: WorkspaceCommand) {
    const timer = setTimeout(() => this.unanswered(d.id), MAP_ANSWER_MS);
    this.waiting.set(d.id, { d, turn, source, timer });
    this.send(action);
  }

  /** The map never answered: the command is withdrawn (it can no longer run behind the
   *  Commander's back), and its line says it was not opened. */
  private unanswered(id: string): void {
    if (!this.waiting.delete(id)) return;
    this.send({ type: "cancel", acks: [id] });
    this.set(id, { state: "unanswered" });
    this.released();
  }

  private turn(id: string) {
    let t = this.turns.get(id);
    if (!t) {
      t = { admitted: 0, fellBack: false, claims: [] };
      this.turns.set(id, t);
    }
    return t;
  }

  // ---- applying one directive -----------------------------------------------------------------

  /** Resolves once the map has answered everything asked of it. LEAVING the map is the one
   *  step that has to wait for the drain: commands still queued there would otherwise sit
   *  unrun behind a page that no longer shows the map. Bounded by `MAP_ANSWER_MS`. */
  private answers(): Promise<void> {
    if (!this.waiting.size) return Promise.resolve();
    return new Promise((resolve) => this.quiet.push(resolve));
  }

  /** Is a directive held back until the map has answered (see `answers`)? For tests, which
   *  must render exactly then and not a moment earlier. */
  get waitingForMap(): boolean {
    return this.quiet.length > 0;
  }

  private released(): void {
    if (this.waiting.size) return;
    for (const resolve of this.quiet.splice(0)) resolve();
  }

  private async go(d: ScreenDirective, to: string, ok: ScreenOutcome, source: ScreenSource) {
    if (this.deps.onMap()) await this.answers();
    if ((await this.deps.navigate(to)) === "held") {
      this.set(d.id, { state: "held" });
      return false;
    }
    this.set(d.id, ok);
    this.deps.shown(source, null);
    return true;
  }

  /** The map on screen, or `false` when an unsaved-changes guard held the way there. */
  private async onMap(d: ScreenDirective): Promise<boolean> {
    if (this.deps.onMap() || (await this.deps.navigate(MAP_PATH)) === "applied") return true;
    this.set(d.id, { state: "held" });
    return false;
  }

  private async window(
    d: ScreenDirective,
    key: string,
    title: string,
    turnId: string,
    source: ScreenSource,
  ): Promise<void> {
    const turn = this.turn(turnId);
    const ws = this.settled();
    const refusal: MapRefusal | null = !this.deps.mapCapable()
      ? "host"
      : holds(ws, key) || ws.windows.length < ws.cap
        ? null
        : "cap";
    if (refusal) {
      // Decided up front. One request falls back to its full-screen route; the rest of a batch
      // is listed with a way in — opened one after another, only the last would stay on screen.
      if (turn.admitted > 0 || turn.fellBack) {
        this.set(d.id, { state: "left", reason: refusal });
        return;
      }
      turn.fellBack = true;
      await this.go(d, matchRoute(key), { state: "fallback", reason: refusal }, source);
      return;
    }
    if (!(await this.onMap(d))) return;
    turn.admitted += 1;
    // Queued, not shown: the workspace's drain decides, and answers this directive's id.
    this.ask(d, turnId, source, {
      type: "request",
      req: { seed: matchSeed({ id: key, title }), acks: [d.id] },
    });
  }

  private async apply(
    d: ScreenDirective,
    title: string,
    turnId: string,
    source: ScreenSource,
  ): Promise<void> {
    if (d.op === "open" && d.where === "map") {
      return this.window(d, d.target.id, title, turnId, source);
    }
    if (d.op === "open" || d.op === "go") {
      await this.go(d, directiveRoute(d), { state: "shown", where: "full" }, source);
      return;
    }
    if (d.op === "map_focus") return this.window(d, d.key, title, turnId, source);
    // The map's commands. Whether there is anything to act on is read from where the workspace
    // will settle; what then happened is the workspace's own answer.
    const ws = this.settled();
    if (d.op === "map_minimize" || d.op === "map_close") {
      if (!holds(ws, d.key)) return this.set(d.id, { state: "none" });
      return this.ask(d, turnId, source, {
        type: d.op === "map_close" ? "close" : "minimize",
        key: d.key,
        ack: d.id,
      });
    }
    if (!ws.windows.length) return this.set(d.id, { state: "none" });
    if (d.op === "map_close_all") {
      return this.ask(d, turnId, source, { type: "closeAll", ack: d.id });
    }
    if (!this.deps.mapCapable()) return this.set(d.id, { state: "none", reason: "host" });
    if (ws.windows.every((w) => w.minimized)) {
      return this.set(d.id, { state: "none", reason: "parked" });
    }
    // Arrange needs the measured map: it is queued for the canvas, in its place in the order.
    if (!(await this.onMap(d))) return;
    this.ask(d, turnId, source, { type: "requestArrange", ack: d.id });
  }

  // ---- what the workspace answered ------------------------------------------------------------

  /** The workspace rendered. Its acknowledgements settle the commands they name; the shadow is
   *  rebuilt from this state the next time it is read. */
  settle(): void {
    if (!this.waiting.size) return;
    for (const ack of this.deps.workspace().acks) {
      const w = this.waiting.get(ack.id);
      if (!w) continue;
      this.waiting.delete(ack.id);
      clearTimeout(w.timer);
      this.answered(w, ack);
    }
    this.released();
  }

  private answered({ d, turn, source }: Waiting, ack: WorkspaceAck): void {
    const key = sessionOf(d);
    if (ack.result === "cancelled") return this.set(d.id, { state: "unanswered" });
    if (d.op === "open" || d.op === "map_focus") {
      if (ack.result === "applied") {
        this.claim(turn, d);
        this.set(d.id, { state: "shown", where: "map" });
        this.deps.shown(source, key);
        return;
      }
      // Refused by the drain itself (the map filled or shrank after the executor looked): one
      // refusal is handed back to the full-screen route by the map, several stay listed there.
      const reason: MapRefusal = ack.reason === "host" ? "host" : "cap";
      return this.set(d.id, { state: ack.alone ? "fallback" : "left", reason });
    }
    if (ack.result === "applied") {
      this.claim(turn, d, ack.parked);
      this.set(d.id, { state: "done" });
      if (d.op === "map_arrange") this.deps.shown(source, null);
      return;
    }
    const reason = ack.reason === "parked" || ack.reason === "host" ? ack.reason : undefined;
    this.set(d.id, reason ? { state: "none", reason } : { state: "none" });
  }

  /** A directive changed the map. What the turn's earlier directives said about the SAME
   *  windows is no longer how the map is, so their outcomes give way (`undone`) and the line
   *  under the answer tells the last word: a session opened and then closed reads "window
   *  closed", not "shown". Close all supersedes every earlier one; an Arrange only the "shown"
   *  of a session it had to park in the tray (`parked`). An Arrange's own line ("windows
   *  arranged") stays true whatever comes after it. */
  private claim(turnId: string, d: ScreenDirective, parked: string[] = []): void {
    const turn = this.turn(turnId);
    const key = sessionOf(d);
    const arrange = d.op === "map_arrange";
    for (const c of turn.claims) {
      if (c.arrange) continue;
      const was = this.outcomes.get(c.id)?.outcome.state;
      const superseded = arrange
        ? was === "shown" && c.key !== null && parked.includes(c.key)
        : d.op === "map_close_all" || (c.key !== null && c.key === key);
      if (superseded && (was === "shown" || was === "done")) this.set(c.id, { state: "undone" });
    }
    turn.claims.push({ id: d.id, key, arrange });
  }

  /** The line's Open, tapped by the Commander: sessions go to the map where it has room for
   *  them, otherwise the FIRST one opens full screen; anything else goes to its route. Never an
   *  execution of the record — a tap is the Commander's own navigation. */
  open(action: ScreenOpen): void {
    const { deps } = this;
    if (action.type === "route") {
      void deps.navigate(action.to);
      return;
    }
    const [first] = action.sessions;
    if (!first) return;
    const ws = this.settled();
    let room = deps.mapCapable() ? ws.cap - ws.windows.length : 0;
    const take = deps.mapCapable()
      ? action.sessions.filter((s) => {
          if (holds(ws, s.key)) return true;
          room -= 1;
          return room >= 0;
        })
      : [];
    if (!take.length) {
      void deps.navigate(matchRoute(first.key));
      return;
    }
    void (async () => {
      if (!deps.onMap() && (await deps.navigate(MAP_PATH)) === "held") return;
      for (const s of take)
        this.send({ type: "request", req: { seed: matchSeed({ id: s.key, title: s.title }) } });
    })();
  }
}
