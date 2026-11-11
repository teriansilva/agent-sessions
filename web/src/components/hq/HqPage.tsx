/** HQ's page (#1776): the conversation list beside the open thread, with a link to each.
 *
 *  The operator asked for a full page a link posted elsewhere can jump straight into (#1746 D12),
 *  not only the sidebar. `/hq` lists the conversations; `/hq/c/<id>` opens one. The thread is the
 *  same `AskConsole` the sidebar hosts, driven through its CONTROLLED selection: the route's id is
 *  the conversation on screen, and the console reports what it did (`created`, `opened`,
 *  `deleted`, `missing`) so the page keeps the URL true —
 *
 *  - the FIRST send of a new conversation replaces `/hq` with `/hq/c/<id>`, so Back never returns
 *    to an empty draft;
 *  - a pick in the list pushes; Back/Forward move between conversations;
 *  - deleting the one on screen replaces it with `/hq`;
 *  - an id the server does not have — unknown, expired, deleted — and one that is not an id at all
 *    render ONE "isn't here" state. A malformed id asks the server nothing (`ASK_CID_RE`).
 *
 *  **One route element** for `/hq` and `/hq/*` (App.tsx), so the console survives the first send's
 *  `/hq` → `/hq/c/<id>`. **One console per conversation:** the shell closes the sidebar on `/hq*`
 *  and its corner icon brings the operator to this composer instead (`HQ_FOCUS_COMPOSER_EVENT`);
 *  a turn the hidden sidebar is still streaming shows here as pending, and the console's Send
 *  waits for it (`busy`).
 *
 *  **Phone (≤800px): master/detail.** `/hq` is the list; a conversation, or New conversation, is
 *  the thread full screen with ‹ back. The console stays mounted (hidden) under the list, so going
 *  back to the list never drops a turn this page is watching.
 *
 *  The list is the shared store's (`askConversationsStore`): the sidebar's switcher and this column
 *  show the same rows, under the same generation guard. Search filters the loaded titles here, in
 *  the browser.
 *
 *  **Dispatches** (#1837): a pinned entry above the conversations, hidden while there are none, with
 *  "N new" since the operator last opened them; it opens the read-only view in the thread column
 *  (`/hq/d`, or `/hq/d/<id>` scrolled to one and marked LINKED). The console stays mounted, hidden,
 *  behind it. */
import { Inbox, Plus, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useConfig } from "../../app/config";
import { api } from "../../lib/api";
import { HQ_DISPATCHES_PATH, HQ_PATH, hqConversationPath } from "../../lib/routes";
import type { HqDispatchList } from "../../types/api";
import { settingsPath } from "../../routes/settingsTabs";
import { useIsMobile } from "../../lib/useIsMobile";
import { AskConsole } from "../ask/AskConsole";
import {
  pageTouched,
  refreshConversations,
  useAskConversations,
} from "../ask/askConversationsStore";
import { HQ_FOCUS_COMPOSER_EVENT } from "../ask/askPanel";
import { nextDispatchRead } from "./dispatchReads";
import { DispatchesView } from "./DispatchesView";
import { hqTarget } from "./hqRoute";
import s from "./HqPage.module.css";

function when(ts: number): string {
  const d = new Date(ts * 1000);
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `Today ${time}`;
  return `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${time}`;
}

/** "N new", as the entry shows it: 50+ above 50. */
const newLabel = (n: number) => (n > 50 ? "50+ new" : `${n} new`);

/** How often an open, visible HQ page re-reads the Dispatches (Hermes 7619, finding 4). */
export const DISPATCH_POLL_MS = 15_000;

/** The Dispatches list behind the pinned entry: re-read on every route change, when the view moved
 *  the cursor, and every `DISPATCH_POLL_MS` while the page is visible (paused while the tab is
 *  hidden, read at once when it comes back — `MissionNowStrip`'s pattern). A read never marks
 *  anything seen. Only the newest read applies, so an older answer never restores a count. */
function useDispatches(key: string): [HqDispatchList | null, number, () => void] {
  const [read, setRead] = useState<{ list: HqDispatchList; seq: number } | null>(null);
  const gen = useRef(0);
  const refresh = useCallback(() => {
    const mine = ++gen.current;
    // Numbered when it STARTS, on the counter the Dispatches view's own reads share.
    const seq = nextDispatchRead();
    api.hqDispatches().then(
      (l) => {
        // Only a list the page can read: anything else keeps the entry hidden.
        if (mine !== gen.current || !Array.isArray(l?.dispatches)) return;
        // A poll that could not read the store keeps the last readable list, marked unreadable
        // (Hermes 7681, finding 4): a passing read failure is never shown as "no dispatches".
        setRead((prev) =>
          l.unreadable
            ? { list: { ...(prev?.list ?? l), unreadable: true }, seq }
            : { list: { ...l, unreadable: false }, seq },
        );
      },
      () => undefined, // the entry keeps what it last showed
    );
  }, []);
  useEffect(() => {
    refresh();
  }, [key, refresh]);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const arm = () => {
      if (!document.hidden) timer = setTimeout(tick, DISPATCH_POLL_MS);
    };
    const tick = () => {
      timer = null;
      refresh();
      arm();
    };
    const onVis = () => {
      if (document.hidden) {
        if (timer) clearTimeout(timer);
        timer = null;
      } else if (!timer) {
        tick();
      }
    };
    arm();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [refresh]);
  return [read?.list ?? null, read?.seq ?? 0, refresh];
}

export default function HqPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const cfg = useConfig();
  const configured = cfg?.pulse?.configured ?? false;
  const conversations = useAskConversations();
  const [query, setQuery] = useState("");

  const target = hqTarget(location.pathname);
  const onDispatches = target.kind === "dispatches";
  const [dispatches, dispatchesSeq, refreshDispatches] = useDispatches(location.key);
  const routeId = target.kind === "conversation" ? target.id : null;
  /** The id the server said it does not have. Cleared as soon as the route names another. */
  const [missingId, setMissingId] = useState<string | null>(null);
  const missing = target.kind === "nothing" || (routeId !== null && routeId === missingId);
  const conversationId = missing ? null : routeId;
  /** New conversation on a phone: the thread, before it has an id. */
  const composing = Boolean((location.state as { hqCompose?: boolean } | null)?.hqCompose);

  useEffect(() => {
    void refreshConversations();
  }, []);
  // The kept sidebar re-reads on reopening once the page has been used (`pageTouched`): on every
  // route change here, and on leaving — the sidebar can only reopen once the page is gone.
  useEffect(() => {
    pageTouched();
  }, [location.key]);
  useEffect(() => () => pageTouched(), []);

  const onChange = useCallback(
    (id: string | null, why: "created" | "opened" | "deleted" | "missing") => {
      if (why === "created" && id) navigate(hqConversationPath(id), { replace: true });
      else if (why === "opened" && id) navigate(hqConversationPath(id));
      else if (why === "deleted") navigate(HQ_PATH, { replace: true });
      else if (why === "missing" && id) setMissingId(id);
    },
    [navigate],
  );

  const showThread = !isMobile || target.kind !== "list" || composing;
  const showList = !isMobile || !showThread;

  // The corner icon on this page (#1776): to the composer, opening the thread first on a phone.
  const pageRef = useRef<HTMLDivElement | null>(null);
  const [focusAsked, setFocusAsked] = useState(0);
  useEffect(() => {
    const on = () => {
      // The Dispatches are read only: the composer is a new conversation's.
      if ((isMobile && !showThread) || onDispatches) navigate(HQ_PATH, { state: { hqCompose: true } });
      setFocusAsked((n) => n + 1);
    };
    window.addEventListener(HQ_FOCUS_COMPOSER_EVENT, on);
    return () => window.removeEventListener(HQ_FOCUS_COMPOSER_EVENT, on);
  }, [isMobile, showThread, onDispatches, navigate]);
  useEffect(() => {
    if (!focusAsked) return;
    pageRef.current?.querySelector<HTMLTextAreaElement>("[data-hq-thread] textarea")?.focus();
  }, [focusAsked, showThread]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = conversations ?? [];
    return q ? all.filter((c) => (c.title || "").toLowerCase().includes(q)) : all;
  }, [conversations, query]);

  const back = isMobile ? () => navigate(HQ_PATH) : undefined;

  return (
    <div className={s.page} ref={pageRef} data-testid="hq-page">
      {showList ? (
        <nav className={s.list} aria-label="HQ conversations" data-testid="hq-list">
          <div className={s.listHead}>
            <h1 className={s.title}>HQ</h1>
            <Link
              className={s.newBtn}
              to={HQ_PATH}
              state={{ hqCompose: true }}
              // Same path as the list: React Router would REPLACE it, and on a phone Back from the
              // new thread must return to the list. A push, there.
              replace={isMobile ? false : undefined}
              data-testid="hq-new"
            >
              <Plus size={16} aria-hidden="true" />
              New conversation
            </Link>
          </div>
          {!configured ? (
            <p className={s.note} data-testid="hq-not-set-up">
              HQ needs an AI endpoint to answer. Kept conversations stay readable. Set one up in{" "}
              <Link to={settingsPath("ai-endpoint")}>Settings → Endpoint &amp; model</Link>.
            </p>
          ) : null}
          <label className={s.search}>
            <Search size={15} aria-hidden="true" />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search conversations"
              aria-label="Search conversations"
              data-testid="hq-search"
            />
          </label>
          {dispatches && (dispatches.dispatches.length > 0 || dispatches.unreadable) ? (
            <Link
              className={`${s.pin} ${onDispatches ? s.rowCur : ""}`}
              to={HQ_DISPATCHES_PATH}
              aria-current={onDispatches ? "page" : undefined}
              data-testid="hq-dispatches-entry"
            >
              <span className={s.pinTitle}>
                <Inbox size={14} aria-hidden="true" /> Dispatches
              </span>
              {dispatches.new > 0 && !dispatches.unreadable ? (
                <span className={s.pinNew} data-testid="hq-dispatches-new-count">
                  {newLabel(dispatches.new)}
                </span>
              ) : (
                <span />
              )}
              <span className={s.pinMeta} data-testid="hq-dispatches-entry-meta">
                {dispatches.unreadable
                  ? "Can't be read right now"
                  : `Written by HQ · last ${when(dispatches.dispatches[0].at)}`}
              </span>
            </Link>
          ) : null}
          {conversations !== null && conversations.length === 0 ? (
            <p className={s.empty} data-testid="hq-list-empty">
              No conversations yet. Ask HQ anything: your first question starts one.
            </p>
          ) : conversations !== null && rows.length === 0 ? (
            <p className={s.empty} data-testid="hq-search-empty">
              No conversation matches “{query.trim()}”.
            </p>
          ) : (
            <ul className={s.rows}>
              {rows.map((c) => {
                const cur = c.id === conversationId;
                return (
                  <li key={c.id}>
                    <Link
                      className={`${s.row} ${cur ? s.rowCur : ""}`}
                      to={hqConversationPath(c.id)}
                      aria-current={cur ? "page" : undefined}
                      data-testid="hq-list-item"
                    >
                      <span className={s.rowTitle}>{c.title || "Untitled conversation"}</span>
                      <span className={s.rowMeta}>
                        {c.unread ? (
                          <span className={s.dot} aria-label="New updates" role="img" />
                        ) : null}
                        {when(c.updated_at)}
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </nav>
      ) : null}
      {onDispatches && showThread ? (
        <DispatchesView
          linkedId={target.id}
          malformed={target.malformed}
          openKey={location.key}
          configured={configured}
          onBack={back}
          onSeen={refreshDispatches}
          latest={dispatches?.dispatches ?? null}
          latestSeq={dispatchesSeq}
          latestUnreadable={dispatches?.unreadable === true}
        />
      ) : null}
      {missing && showThread ? (
        <section className={s.missing} aria-labelledby="hq-missing-title" data-testid="hq-missing">
          {back ? (
            <button type="button" className={s.backLink} onClick={back} data-testid="hq-missing-back">
              ‹ Conversations
            </button>
          ) : null}
          <h2 id="hq-missing-title" className={s.missingTitle}>
            This conversation isn&apos;t here
          </h2>
          <p className={s.missingText}>
            It may have been deleted, or the link is not one HQ made. Your other conversations are
            in the list.
          </p>
          <Link className={s.newBtn} to={HQ_PATH} state={{ hqCompose: true }}>
            <Plus size={16} aria-hidden="true" />
            New conversation
          </Link>
        </section>
      ) : null}
      {/* Always mounted (module note); hidden — and inert — while the list or the missing state
          is what this screen shows. */}
      <div
        className={s.thread}
        hidden={!showThread || missing || onDispatches}
        data-hq-thread=""
        data-testid="hq-thread"
      >
        <AskConsole
          chrome="page"
          configured={configured}
          visible={showThread && !missing && !onDispatches}
          conversationId={conversationId}
          onConversationChange={onChange}
          onBack={back}
        />
      </div>
    </div>
  );
}
