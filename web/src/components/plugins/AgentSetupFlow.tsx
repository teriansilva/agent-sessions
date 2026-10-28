import { AgentCatalogStatus } from "../agents/AgentCatalogStatus";
import { useCallback, useEffect, useRef, useState, type ComponentType, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useConfigRefresh } from "../../app/config";
import { reloadRoster } from "../../app/reloadRoster";
import { AgentEndpointCard } from "../settings/AgentEndpointCard";
import { manifestVersion } from "../agents/agentStatus";
import type { WizardStep } from "../wizard/WizardShell";
import { SignInTerminal } from "./SignInTerminal";
import { AGENTS_PATH, pendingOperation, pluginError, rejectedOperation, missingOperation, setupPath, sourceLabel, verifiedGeneration } from "./model";
import { api } from "../../lib/api";
import type { PluginCatalog, PluginGeneration, PluginOperation, PluginReview } from "../../types/plugins";
import button from "../ui/actionButton.module.css";
import s from "./PluginSetup.module.css";

type Current = <T>(promise: Promise<T>) => Promise<T>;

const STEPS = ["Select", "Review", "Sign in", "Verify", "Enable"].map(label => ({ id: label, label }));
const HEADINGS = ["Choose an agent", "Review this installation", "Connect your account", "Verify the installation", "Ready to enable"];

/** What a host draws around the flow: the route draws `WizardShell` with its leave guard; the setup
 *  wizard's Agents step draws the compact embedded frame below. */
export interface SetupFrame {
  steps: readonly WizardStep[];
  /** The current step's index. */
  at: number;
  heading: string;
  onBack?: () => void;
  onNext?: () => void;
  nextLabel: string;
  nextDisabled: boolean;
  /** Something would be lost by leaving: the route's leave guard. */
  dirty: boolean;
  leaveTitle: string;
  leaveMessage: string;
  /** The installation is enabled: there is nothing left to do here. */
  enabled: boolean;
  body: ReactNode;
  /** Embedded: close the flow. */
  onCancel?: () => void;
}

/** THE Add-agent flow (#1739, extracted from the `/settings/agents/setup/new` route): Select →
 *  Review → Sign in → Verify → Enable, state-driven, with no router calls of its own.
 *
 *  Its state is a setup QUERY (`?plugin=…&generation=…&operation=…`). Query IDs locate durable
 *  server records: opening a query only reads, it never replays work. The route passes the URL's
 *  query (`search`) and replaces it on every pin (`onNavigate`), so a reload or Back reads the same
 *  operation; an embedded host passes nothing and the flow keeps the query in its own state, while
 *  the server keeps the operation (readable later from Settings → Agents). */
export function AgentSetupFlow({
  plugin,
  query,
  search: hostSearch,
  onNavigate,
  onDone,
  onCancel,
  onSigninLive,
  frame: Frame = EmbeddedFrame,
}: {
  /** Open on this agent. Embedded, the agent is fixed: the Select step offers no other. */
  plugin?: string;
  /** Embedded: the setup query to open with (a status action's), e.g. `?operation=…`. */
  query?: string;
  /** Route: the URL's query, which this flow then follows (Back / Forward is a fresh view). */
  search?: string;
  /** Route: replace the URL with this setup path. Absent ⇒ the query lives in this flow's state. */
  onNavigate?: (path: string) => void;
  /** Called once the installation has been enabled here. */
  onDone?: () => void;
  /** Embedded: told whether a sign-in terminal is live. Unmounting the flow then closes the
   *  terminal and interrupts the vendor's sign-in, so a host confirms before it leaves (#1739). */
  onSigninLive?: (live: boolean) => void;
  /** Embedded: close the flow. The server operation, if any, continues. */
  onCancel?: () => void;
  /** The host frame drawn around the flow (the route's `WizardShell`). Absent ⇒ the embedded frame. */
  frame?: ComponentType<SetupFrame>;
}) {
  const [ownSearch_, setOwnSearch] = useState(() => query ?? (plugin ? `?${new URLSearchParams({ plugin })}` : ""));
  const search = hostSearch ?? ownSearch_;
  // Embedded, the agent is the host's (the row's) — fixed for the flow's whole life, whatever the
  // query says: a "View operation" query names no agent, and a retry after a failure must not
  // turn one agent's row into a picker for another (Hermes 7251).
  const lockedPlugin = hostSearch === undefined ? plugin : undefined;
  const locked = !!lockedPlugin;
  const refreshConfig = useConfigRefresh();
  const [catalog, setCatalog] = useState<PluginCatalog | null>(null);
  const [selected, setSelected] = useState(() => lockedPlugin ?? new URLSearchParams(search).get("plugin") ?? "");
  const [mode, setMode] = useState<"catalog" | "local">("catalog");
  const [local, setLocal] = useState("");
  const [adopted, setAdopted] = useState("");
  const [reviewDraft, setReview] = useState<PluginReview | null>(null);
  const [generation, setGeneration] = useState<string | null>(null);
  const [operation, setOperation] = useState<PluginOperation | null>(null);
  const [unknownOperation, setUnknownOperation] = useState<string | null>(() => new URLSearchParams(search).get("operation"));
  const [step, setStep] = useState(0);
  const [localConfirmed, setLocalConfirmed] = useState(false);
  const [adoptConfirmed, setAdoptConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const routeEpoch = useRef(0);
  const ownSearch = useRef<string | null>(null);
  const navigateSetup = useCallback((path: string) => {
    const next = path.includes("?") ? path.slice(path.indexOf("?")) : "";
    ownSearch.current = next;
    if (onNavigate) onNavigate(path);
    else setOwnSearch(next);
  }, [onNavigate]);
  useEffect(() => () => { routeEpoch.current += 1; }, []);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState<string | null>(null);
  // The sign-in terminal outlives its command (its last screen is the only diagnosis), so "a
  // terminal is shown" and "a sign-in is running" are separate facts, keyed by the operation.
  const [signinEnded, setSigninEnded] = useState<string | null>(null);
  const signinLive = !!connected && signinEnded !== connected;
  useEffect(() => {
    onSigninLive?.(signinLive);
  }, [signinLive, onSigninLive]);
  useEffect(() => () => onSigninLive?.(false), [onSigninLive]);
  const [enabled, setEnabled] = useState(false);
  // The one-click update (#1741): `?update=1` opens on ONE confirmation for the catalog entry shown;
  // confirming chains install → verify (with the host's existing vendor login: no sign-in step) →
  // enable. The chain lives in memory only — a reload reads the operation back and continues
  // nothing on its own — and stops at the first step that does not pass, leaving the active
  // generation as it was.
  const updateMode = new URLSearchParams(search).get("update") === "1";
  const [auto, setAuto] = useState(false);
  const [updateFrom, setUpdateFrom] = useState<string | null>(null);
  const [binding, setBinding] = useState<{ active: string | null; revision: string | null } | null>(null);
  // The roster moved since the update was confirmed (another tab disabled, removed or changed the
  // agent): a manual Enable shows what it is now and needs its own confirmation against THAT.
  const [reconfirm, setReconfirm] = useState<{ active: string | null; revision: string | null; enabled: boolean; version: string | null; since: boolean } | null>(null);
  const chained = useRef<string | null>(null);
  const review = (operation && operation.kind !== "install" ? catalog?.plugins
    .find(p => p.id === operation.plugin_id)?.generations?.find(g => g.id === operation.generation_id)?.review : null) ?? reviewDraft;
  const pinned: PluginGeneration | undefined = catalog?.plugins
    .find(p => p.id === review?.plugin_id)?.generations?.find(g => g.id === generation);
  const installation = catalog?.plugins.find(p => p.id === review?.plugin_id);
  const endpointFrozen = !!generation && (installation?.active === generation || !!catalog?.operations.some(op =>
    op.plugin_id === review?.plugin_id && op.generation_id === generation && op.kind === "activate" && op.state === "complete"));
  const isEnabled = enabled || installation?.enabled === true && installation.active === generation;
  const pending = pendingOperation(operation);
  const isChat = review?.entry.manifest.runtime?.kind === "chat";
  // A native API client (#1311) drives an already-installed console agent: nothing to sign in to
  // or run of its own, and its one check is the adapter's readiness against that agent.
  const apiSource = review?.entry.manifest.runtime?.kind === "api" ? review.entry.manifest.api?.source ?? "its console agent" : null;
  const hasSignin = ["cli-subcommand", "auth-login", "interactive"].includes(review?.entry.manifest.signin?.kind ?? "none");

  const accept = useCallback((op: PluginOperation) => {
    setOperation(op); setUnknownOperation(null);
    if (op.review) setReview(op.review);
    if (op.generation_id) setGeneration(op.generation_id);
    if (op.kind === "install") setStep(op.state === "installed" ? 2 : 1);
    if (op.kind === "signin") setStep(op.state === "complete" ? 3 : 2);
    if (op.kind === "verify") setStep(op.state === "verified" ? 4 : 3);
    if (op.kind === "activate" && op.state === "complete") { setStep(4); setEnabled(true); }
    // A confirmed update (#1741) continues only through passes; any other settled outcome ends it.
    const passed = op.kind === "install" ? op.state === "installed" : op.kind === "verify" ? op.state === "verified" : false;
    if (!pendingOperation(op) && !passed) setAuto(false);
  }, []);
  const loadCatalog = useCallback(async () => {
    const epoch = routeEpoch.current;
    const result = await api.plugins();
    if (epoch === routeEpoch.current) setCatalog(result);
    return result;
  }, []);
  const forgetMissing = useCallback((plugin: string, gen: string | null) => {
    setUnknownOperation(null); setOperation(null); setConnected(null); setEnabled(false);
    if (!gen) {
      setGeneration(null); setReview(null); setStep(0);
      setLocalConfirmed(false); setAdoptConfirmed(false);
    } else {
      const epoch = routeEpoch.current;
      void api.plugins().then(result => {
        if (epoch !== routeEpoch.current) return;
        setCatalog(result);
        const found = result.plugins.find(p => p.id === plugin)?.generations?.find(g => g.id === gen);
        if (found) { setGeneration(gen); setReview(found.review); setStep(verifiedGeneration(found) ? 4 : 2); }
      }).catch(error => { if (epoch === routeEpoch.current) setError(pluginError(error)); });
    }
    setError("No operation was created. Review or retry this installation.");
    navigateSetup(setupPath({ ...(plugin ? { plugin } : {}), ...(gen ? { generation: gen } : {}) }));
  }, [navigateSetup]);
  useEffect(() => {
    // Only our own operation-ID replacement belongs to the pending request. Back/Forward
    // to any other query starts a fresh view, even while that request is still running.
    const owned = ownSearch.current === search;
    ownSearch.current = null;
    if (owned) return;
    routeEpoch.current += 1;
    sending.current = false; setBusy(false);
    let alive = true;
    const query = new URLSearchParams(search);
    const op = query.get("operation"), gen = query.get("generation"), id = query.get("plugin");
    setSelected(lockedPlugin ?? id ?? ""); setMode("catalog"); setLocal(""); setAdopted("");
    setOperation(null); setUnknownOperation(op); setGeneration(null); setReview(null);
    setConnected(null); setEnabled(false); setStep(0); setError("");
    setLocalConfirmed(false); setAdoptConfirmed(false); setAuto(false); setUpdateFrom(null); setBinding(null); setReconfirm(null);
    void api.plugins().then(result => {
      if (!alive) return;
      setCatalog(result);
      if (!op && gen && id) {
        const found = result.plugins.find(p => p.id === id)?.generations?.find(g => g.id === gen);
        if (!found) { setError("This installation is unavailable. Return to the gallery and select it again."); return; }
        setGeneration(gen); setReview(found.review); setStep(verifiedGeneration(found) ? 4 : 2);
      }
    }).catch(e => alive && setError(pluginError(e)));
    if (op) {
      void api.pluginOperation(op).then(result => { if (alive) accept(result); })
        .catch(error => {
          if (!alive) return;
          if (missingOperation(error)) forgetMissing(id ?? "", gen);
          else setError("The operation could not be read. Check its status before starting again.");
        });
    }
    return () => { alive = false; };
  }, [search, accept, forgetMissing, lockedPlugin]);
  useEffect(() => {
    if (!operation || !pendingOperation(operation)) return;
    let alive = true;
    const epoch = routeEpoch.current;
    const timer = setInterval(() => {
      void api.pluginOperation(operation.id).then(async op => {
        if (!alive || epoch !== routeEpoch.current) return;
        accept(op);
        if (!pendingOperation(op)) await loadCatalog();
      }).catch(e => alive && epoch === routeEpoch.current && setError(pluginError(e)));
    }, 1200);
    return () => { alive = false; clearInterval(timer); };
  }, [operation, accept, loadCatalog]);

  const run = async (work: (current: Current) => Promise<void>) => {
    if (sending.current) return;
    const epoch = routeEpoch.current;
    const current: Current = async promise => {
      try {
        const result = await promise;
        if (epoch !== routeEpoch.current) throw new Error("Setup target changed");
        return result;
      } catch (error) {
        if (epoch !== routeEpoch.current) throw new Error("Setup target changed", { cause: error });
        throw error;
      }
    };
    sending.current = true; setBusy(true); setError("");
    try { await work(current); } catch (e) { if (epoch === routeEpoch.current) setError(pluginError(e)); }
    finally { if (epoch === routeEpoch.current) { sending.current = false; setBusy(false); } }
  };
  const pinOperation = (id: string, plugin: string, gen?: string) => {
    setUnknownOperation(id);
    navigateSetup(setupPath({ plugin, ...(gen ? { generation: gen } : {}), operation: id }));
  };
  const submit = async (current: Current, id: string, plugin: string, gen: string | undefined, post: () => Promise<PluginOperation>) => {
    pinOperation(id, plugin, gen);
    try { return await current(post()); } catch (error) {
      if (rejectedOperation(error)) {
        // A 409 can also follow a committed operation whose roster refresh failed. Read first.
        try { return await current(api.pluginOperation(id)); } catch (lookup) {
          if (missingOperation(lookup)) forgetMissing(plugin, gen ?? null);
        }
      }
      throw error;
    }
  };
  const readOperation = () => run(async current => {
    const id = unknownOperation ?? operation?.id;
    if (!id) return;
    try { accept(await current(api.pluginOperation(id))); await current(loadCatalog()); }
    catch (error) {
      if (!missingOperation(error)) throw error;
      const query = new URLSearchParams(search);
      forgetMissing(review?.plugin_id ?? operation?.plugin_id ?? query.get("plugin") ?? selected,
        generation ?? query.get("generation"));
    }
  });
  const prepare = () => run(async current => {
    setReview(null); setLocalConfirmed(false); setAdoptConfirmed(false);
    const reviewed = await current(api.pluginReview({ ...(mode === "local" ? { local: JSON.parse(local) as unknown } : { plugin_id: selected }), ...(adopted.trim() ? { adopted_path: adopted.trim() } : {}) }));
    setReview(reviewed); setStep(1);
  });
  const install = () => run(async current => {
    if (!review) return;
    const id = crypto.randomUUID();
    accept(await submit(current, id, review.plugin_id, undefined, () => api.pluginInstall({ request_id: id, review_id: review.id, digest: review.digest,
      confirm_local: localConfirmed, confirm_adopted: adoptConfirmed })));
    await current(loadCatalog());
  });
  const begin = async (current: Current, kind: "signin" | "verify" | "activate", bound?: { active: string | null; revision: string | null }) => {
    if (!review || !generation) return;
    const id = crypto.randomUUID();
    const body = { request_id: id, plugin_id: review.plugin_id, generation_id: generation };
    // A confirmed update enables only while the roster is still the one it was confirmed against
    // (#1741): a disable, removal or other activation since then makes the server refuse it.
    // Every activation from this flow is bound (#1741): there is no unconditioned enable path.
    if (kind === "activate" && !bound) throw new Error("Enabling needs a confirmed agent state; reload and try again.");
    const activation = bound ? { ...body, expected_active: bound.active, expected_revision: bound.revision } : body;
    const op = await submit(current, id, review.plugin_id, generation, () => kind === "signin" ? api.pluginSignin(body) : kind === "verify" ? api.pluginVerify(body) : api.pluginActivate(activation));
    accept(op);
    if (kind === "signin" && op.state === "ready") setConnected(id);
    if (kind === "activate") { await current(api.pluginReload()); await current(reloadRoster()); refreshConfig(); }
    await current(loadCatalog());
    if (kind === "activate" && op.state === "complete") onDone?.();
  };
  const start = (kind: "signin" | "verify" | "activate", bound?: { active: string | null; revision: string | null }) =>
    run(current => begin(current, kind, bound));
  // A manual Enable (#1741, Hermes 7290) is never unconditioned: it re-reads the roster and binds
  // the activation to what it finds. For an update candidate that state must still be the one the
  // update was confirmed against — if it moved, nothing is sent until the operator confirms again
  // against the new state.
  const enableNow = () => run(async current => {
    if (!review) return;
    const fresh = await current(api.plugins());
    setCatalog(fresh);
    const row = fresh.plugins.find(p => p.id === review.plugin_id);
    const now = { active: row?.active ?? null, revision: fresh.roster_revision };
    // THE rule (#1741, Hermes 7290/7299): an UPDATE candidate — the agent already has another
    // generation as its current one — is enabled only with a binding the operator confirmed in
    // THIS mount, against the state shown then, and only while that state still holds. No binding
    // (a reload, a resumed or recovered operation, an error) or a moved state always asks again,
    // showing the state as it is now. A first installation, or re-enabling the very generation that
    // is current, has nothing to replace: it is bound to the state just read.
    const updateCandidate = !!row?.active && row.active !== generation;
    if (updateCandidate && (!binding || !row?.enabled || now.active !== binding.active || now.revision !== binding.revision)) {
      const gen = row?.generations?.find(g => g.id === row.active);
      setReconfirm({ ...now, enabled: !!row?.enabled, version: manifestVersion(gen?.review.entry.manifest), since: !!binding });
      return;
    }
    await begin(current, "activate", updateCandidate ? binding! : now);
  });
  const enableAgain = () => {
    if (!reconfirm) return;
    const bound = { active: reconfirm.active, revision: reconfirm.revision };
    setBinding(bound);
    setReconfirm(null);
    void start("activate", bound);
  };
  const entryShown = catalog?.catalog.find(c => c.manifest.identity.id === selected);
  const selectedRow = catalog?.plugins.find(p => p.id === selected);
  const activeGen = selectedRow?.generations?.find(g => g.id === selectedRow.active);
  const versionNow = manifestVersion(activeGen?.review.entry.manifest);
  const versionNext = manifestVersion(entryShown?.manifest);
  // An update exists only for an ENABLED agent whose active generation is not the catalog's recipe:
  // a stale or hand-made `?update=1` (the agent was disabled or removed meanwhile, or is already
  // current) offers nothing to confirm.
  const updatable = !!selectedRow?.enabled && !!activeGen && !!entryShown && entryShown.installable !== false &&
    entryShown.digest !== activeGen.review.recipe_digest;
  const updateStep = updateMode && step === 0 && !operation && !unknownOperation && !auto && !!selected;
  const showUpdate = updateStep && updatable;
  const update = () => run(async current => {
    if (!entryShown) return;
    setReview(null);
    try {
      // Confirmed against what is on screen — re-read now, so a disable or removal in another tab
      // since the page loaded stops here, before anything is installed.
      const fresh = await current(api.plugins());
      setCatalog(fresh);
      const row = fresh.plugins.find(p => p.id === selected);
      const now = row?.generations?.find(g => g.id === row.active);
      if (!row?.enabled || !now || row.active !== activeGen?.id || fresh.catalog.find(c => c.manifest.identity.id === selected)?.digest !== entryShown.digest) {
        throw new Error("This agent changed since the update was shown (it was disabled, removed or updated). Nothing was installed.");
      }
      setBinding({ active: now.id, revision: fresh.roster_revision });
      const reviewed = await current(api.pluginReview({ plugin_id: selected }));
      // The confirmation covered the entry on screen. A server review of anything else — a newer
      // cut landed meanwhile, or a source that would need its own consent — installs nothing.
      if (reviewed.recipe_digest !== entryShown.digest || reviewed.source === "local" || reviewed.adopted_path) {
        throw new Error("The agent catalog changed since this update was shown. Nothing was installed; check the update again.");
      }
      setReview(reviewed);
      setUpdateFrom(versionNow);
      setAuto(true);
      const id = crypto.randomUUID();
      accept(await submit(current, id, reviewed.plugin_id, undefined, () => api.pluginInstall({ request_id: id, review_id: reviewed.id, digest: reviewed.digest,
        confirm_local: false, confirm_adopted: false })));
      await current(loadCatalog());
    } catch (e) {
      setAuto(false);
      throw e;
    }
  });
  // Each finished step of a confirmed update starts the next — once per operation, never while
  // another request is in flight. Anything but a pass ends the chain where it stands.
  useEffect(() => {
    if (!auto || busy || !operation || chained.current === operation.id) return;
    if (operation.kind === "install" && operation.state === "installed" && generation) {
      chained.current = operation.id;
      void start("verify");
    } else if (operation.kind === "verify" && operation.state === "verified") {
      if (!verifiedGeneration(pinned)) return; // its results are still being read
      chained.current = operation.id;
      if (binding) void start("activate", binding);
    }
    // `start` is re-made every render from the state listed here; the guard above runs it once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto, busy, operation, generation, pinned, binding]);
  const recover = () => run(async current => {
    setCatalog(await current(api.pluginRecover()));
    if (operation) accept(await current(api.pluginOperation(operation.id)));
  });
  const reset = () => {
    setReview(null); setGeneration(null); setOperation(null); setUnknownOperation(null);
    setLocalConfirmed(false); setAdoptConfirmed(false); setError(""); setStep(0);
    navigateSetup(setupPath({ plugin: selected }));
  };
  const canInstall = !!review && !pending && !unknownOperation && !busy &&
    (review.source === "signed" || localConfirmed) && (!review.adopted_path || adoptConfirmed);
  const unresolved = pending || !!unknownOperation;
  const dirty = !enabled && (busy || unresolved || !!review || !!local || !!adopted);

  const body = <div className={s.stack}>
      {error && <div role="alert" className={`${s.note} ${s.error}`}>{error}</div>}
      {unknownOperation && <div className={s.note}><p>The request outcome has not been read. Reloading this page will only check the same operation.</p>
        <button className={button.ghost} onClick={() => void readOperation()} disabled={busy}>Check operation status</button></div>}
      {showUpdate && <section className={s.panel} aria-label="Update summary">
        <dl className={s.kv}>
          <dt>Version</dt><dd data-testid="update-versions">{versionNow ?? "not installed"} → <b>{versionNext ?? "?"}</b></dd>
          <dt>Source</dt><dd>{entryShown?.manifest.install?.authority ?? entryShown?.manifest.identity.publisher}{" · "}{entryShown?.source === "bundled" ? "bundled with BattleLab" : `signed catalog${catalog?.feed.sequence ? ` cut #${catalog.feed.sequence}` : ""}`}</dd>
          <dt>Account</dt><dd>Keeps this host’s {entryShown?.manifest.identity.label} sign-in. No new sign-in unless verification needs one.</dd>
          <dt>Running sessions</dt><dd>Keep their current version until they restart.</dd>
          <dt>Verification</dt><dd>Runs the new version in a separate workspace before anything switches: it starts and resumes a test conversation, can access host files, create vendor history, contact the vendor and consume quota.</dd>
        </dl>
        <details className={s.details}><summary>Pinned recipe</summary><p className={s.digest}>Recipe: {entryShown?.digest}</p></details>
        <p className={s.note}>If verification fails, nothing changes: {versionNow ?? "the current version"} stays active.</p>
        {entryShown?.reason && <p className={s.note}>{entryShown.reason}</p>}
      </section>}
      {auto && <p role="status" aria-live="polite" data-testid="update-progress">Updating {entryShown?.manifest.identity.label ?? selected} to {versionNext}: {operation?.kind === "verify" ? "verifying with this host’s sign-in" : operation?.kind === "activate" ? "enabling" : "installing"}…</p>}
      {updateFrom && !auto && !isEnabled && operation && ["failed", "interrupted"].includes(operation.state) && <p className={s.note} data-testid="update-failed">
        {operation.kind === "verify" ? "Verification didn’t pass, so nothing changed" : "The update didn’t complete, so nothing changed"}: {updateFrom} stays active.
        {operation.kind === "verify" && hasSignin && " If it failed on the vendor sign-in, use Back to account setup to sign in, then verify again."}</p>}
      {updateStep && !updatable && catalog && <p className={s.note} role="status" data-testid="update-unavailable">
        There is no update to confirm: {entryShown?.manifest.identity.label ?? selected} is {!selectedRow?.enabled || !activeGen ? "not enabled on this host" : "already on the catalog’s version"}. Nothing will be installed.</p>}
      {step === 0 && !updateStep && locked && <p>Review the catalog entry for <b>{catalog?.catalog.find(c => c.manifest.identity.id === selected)?.manifest.identity.label ?? selected}</b>: its source, pinned artifacts and what it can access, before anything is installed.</p>}
      {step === 0 && !updateStep && locked && catalog?.catalog.find(c => c.manifest.identity.id === selected)?.reason && <p className={s.note}>{catalog.catalog.find(c => c.manifest.identity.id === selected)?.reason}</p>}
      {step === 0 && !updateStep && !locked && <>
        <p>Choose an agent catalog entry or review a local manifest and its pinned artifact recipe.</p>
        <div className={s.row}>
          {(["catalog", "local"] as const).map(value => <button key={value} className={`${button.ghost} ${s.filter}`} aria-pressed={mode === value} onClick={() => setMode(value)}>{value === "catalog" ? "Agent catalog" : "Local manifest"}</button>)}
        </div>
        {mode === "catalog" ? <>
          <label className={s.field}><span>Agent</span><select className={s.input} value={selected} onChange={e => setSelected(e.target.value)}>
            <option value="">Select an agent</option>{catalog?.catalog.map(c => <option key={c.manifest.identity.id} value={c.manifest.identity.id} disabled={c.installable === false}>{c.manifest.identity.label} · {c.manifest.identity.version}{c.included ? " · Included with BattleLab" : c.installable === false ? " · Unavailable" : ""}</option>)}
          </select></label>
          {catalog?.feed.error && <p className={s.note} role="alert">{catalog.feed.error}</p>}
          {catalog?.catalog.find(c => c.manifest.identity.id === selected)?.reason && <p className={s.note}>{catalog.catalog.find(c => c.manifest.identity.id === selected)?.reason}</p>}
          <AgentCatalogStatus feed={catalog?.feed} busy={busy}
            refresh={() => void run(async current => {
              try { setCatalog(await current(api.pluginRefresh())); }
              catch (error) { setCatalog(await current(api.plugins())); throw error; }
            })}
            configure={value => void run(async current => setCatalog(await current(api.agentCatalogPreferences(value))))} />
          {!!catalog?.catalog.some(c => c.included) && <p>API agents are included with BattleLab and use their source agent’s installation. Configure them in <Link to={AGENTS_PATH}>Agents</Link>.</p>}
        </> : <>
          <p className={s.note}>Local manifests are untrusted. Verification does not make their publisher trusted.</p>
          <label className={s.field}><span>Manifest and recipe JSON</span><textarea className={s.input} value={local} onChange={e => setLocal(e.target.value)} spellCheck={false} autoComplete="off" /></label>
        </>}
        <label className={s.field}><span>Existing binary path (optional)</span><input className={s.input} value={adopted} onChange={e => setAdopted(e.target.value)} placeholder="/absolute/path/to/agent" autoComplete="off" /></label>
        <p>Leave the path empty to install a separate copy. Adoption uses the existing file and requires a separate confirmation of its digest.</p>
      </>}
      {review && <section className={s.panel} aria-label="Reviewed installation">
        <h2>{review.entry.manifest.identity.label} <small>{review.entry.manifest.identity.version}</small></h2>
        <p>{sourceLabel(review)} · {review.entry.manifest.identity.publisher}</p>
        {review.source === "local" && <p className={s.note}>Untrusted local source. Only continue if you trust these exact files and their publisher.</p>}
        <dl className={s.kv}>
          <dt>Installation</dt><dd>{review.adopted_path ?? review.entry.manifest.install?.kind ?? "API agent"}</dd>
          <dt>Access</dt><dd>{isChat ? "The configured endpoint receives the test message and later conversations." : apiSource ? `Drives the installed ${apiSource} CLI through its structured protocol, with that agent's login and config. You approve each request it makes.` : "Vendor code runs as your host account and can access its files and network."}</dd>
          <dt>After install</dt><dd>Disabled until verification passes and you explicitly enable it. Existing sessions, credentials and previous installations stay.</dd>
        </dl>
        <details className={s.details}><summary>Source and pinned digests</summary>
          <p className={s.digest}>Review: {review.digest}<br />Recipe: {review.recipe_digest}</p>
          {review.adopted_path && <p className={s.digest}>{review.adopted_path}<br />SHA-256: {review.adopted_sha256}</p>}
          {review.entry.recipe.artifacts.map((artifact, i) => <p key={i} className={s.digest}>{artifact.url}<br />SHA-256: {artifact.sha256}<br />Destination: {artifact.destination}</p>)}
        </details>
        {step === 1 && !operation && !unknownOperation && <>
          {review.source === "local" && <label className={s.check}><input type="checkbox" checked={localConfirmed} onChange={e => setLocalConfirmed(e.target.checked)} />I trust this exact local manifest and artifact recipe.</label>}
          {review.adopted_path && <label className={s.check}><input type="checkbox" checked={adoptConfirmed} onChange={e => setAdoptConfirmed(e.target.checked)} />Use this existing binary at the displayed path and digest.</label>}
        </>}
      </section>}
      {operation && <section className={s.panel} aria-label="Operation status" aria-live="polite">
        <h2>{operation.kind} · {operation.state.replaceAll("_", " ")}</h2>
        <p className={s.digest}>Operation {operation.id}</p>
        {operation.error && <p role="alert">{operation.error}</p>}
        {pending && <p>{operation.state === "ready" ? "Ready for an explicit sign-in connection." : "This operation belongs to the server. Returning here will read its status without submitting it again."}</p>}
        {operation.state === "cleanup_pending" && <button className={button.ghost} disabled={busy} onClick={() => void recover()}>Retry cleanup</button>}
        {operation.kind === "install" && ["failed", "interrupted"].includes(operation.state) && <button className={button.ghost} onClick={reset}>Review a new attempt</button>}
      </section>}
      {step === 2 && !auto && review && generation && <>
        {isChat ? <AgentEndpointCard engine={review.plugin_id} generation={generation} readOnly={endpointFrozen} onSaved={async () => {
          setCatalog(current => current && ({ ...current, plugins: current.plugins.map(p => ({ ...p, generations: p.generations?.map(g => g.id === generation ? { ...g, verification: null } : g) })) }));
          setEnabled(false); await loadCatalog();
        }} /> : apiSource ? <p>Nothing to sign in to: this client uses the {apiSource} agent's own login on this host.</p> : <>
          <p>Sign in with the vendor in this temporary terminal. BattleLab does not save its input or output. The vendor can store credentials in its own account files.</p>
          <p>If the vendor asks you to trust this folder, make that choice here. Verification uses the same private workspace.</p>
          {hasSignin ? <div className={s.row}>
            <button className={button.primary} disabled={busy || signinLive || unresolved && operation?.state !== "ready"} onClick={() => {
              if (operation?.kind === "signin" && operation.state === "ready") setConnected(operation.id);
              else void start("signin");
            }}>{operation?.state === "ready" ? "Connect sign-in terminal" : "Start sign-in"}</button>
            {connected && <button className={button.ghost} onClick={() => { setConnected(null); }}>Close sign-in terminal</button>}
            {operation?.state === "ready" && !connected && <button className={button.ghost} disabled={busy} onClick={() => void run(async current => accept(await current(api.pluginCancel(operation.id))))}>Cancel sign-in attempt</button>}
          </div> : <p>This manifest has no separate sign-in command. Verification checks the vendor account already configured on this host.</p>}
          {connected && <SignInTerminal key={connected} operation={connected} onEnded={() => { setSigninEnded(connected); void readOperation(); }} />}
        </>}
        <button className={button.primary} disabled={busy || unresolved || signinLive} onClick={() => setStep(3)}>Continue to verification</button>
      </>}
      {(step === 3 || step === 4) && review && generation && <>
        <section className={s.panel}><h2>Required checks</h2>
          <ul className={s.checks}>{pinned?.required_checks.map(check => {
            const result = pinned.verification?.results.find(r => r.check === check);
            return <li key={check}><span>{check}<small>{result?.detail ?? "Not run for this installation."}</small></span><b>{result ? result.passed ? "Passed" : "Failed" : "Pending"}</b></li>;
          })}</ul>
          {!pinned && <p>Loading this installation’s required checks…</p>}
        </section>
        {!isEnabled && <>
          <p className={s.note}>{isChat ? "Verification sends a fixed test message to the saved endpoint and can consume API quota."
            : apiSource ? `Verification checks that ${apiSource} is installed, supports native mode and can be contained. It starts nothing and sends nothing to a model.`
            : "Verification runs the agent in a separate workspace, starts and resumes a test conversation, reads its transcript and checks usage where supported. It can access host files, create vendor history, contact the vendor and consume quota."}</p>
          <div className={s.row}>
            <button className={button.ghost} disabled={busy || unresolved} onClick={() => setStep(2)}>Back to account setup</button>
            <button className={button.primary} disabled={busy || unresolved || !pinned} onClick={() => void start("verify")}>{pinned?.verification ? "Run verification again" : "Verify installation"}</button>
          </div>
          {verifiedGeneration(pinned) && <><p className={s.ok}>All required checks passed for this exact installation. {review.source === "local" && "Its source remains local and untrusted."}</p>
            {!reconfirm && <button className={button.primary} disabled={busy || unresolved} onClick={() => void enableNow()}>Enable agent</button>}
            {reconfirm && <section className={s.panel} aria-label="Confirm again" data-testid="update-reconfirm">
              <p>{reconfirm.since
                ? <>Since you confirmed this update, {review.entry.manifest.identity.label} {!reconfirm.enabled ? "was disabled or removed" : `changed to ${reconfirm.version ?? "another version"}`}.</>
                : <>{review.entry.manifest.identity.label} is {!reconfirm.enabled ? `disabled on this host (last on ${reconfirm.version ?? "another version"})` : `on ${reconfirm.version ?? "another version"}`}.</>}
                {" "}{!reconfirm.enabled
                  ? <b>Enabling {manifestVersion(review.entry.manifest) ?? "this version"} re-enables an agent that is disabled.</b>
                  : <>Enabling {manifestVersion(review.entry.manifest) ?? "this version"} replaces it; running sessions keep it until they restart.</>}</p>
              <div className={s.row}>
                <button className={button.ghost} disabled={busy} onClick={() => setReconfirm(null)}>Leave it as it is</button>
                <button className={button.primary} disabled={busy || unresolved} onClick={enableAgain}>Enable {manifestVersion(review.entry.manifest) ?? "this version"} anyway</button>
              </div>
            </section>}</>}
        </>}
        {isEnabled && <><p className={s.ok}>Agent enabled. Your saved defaults and budgets are unchanged.</p>
          <button className={button.ghost} disabled={busy} onClick={() => void run(async current => { await current(api.pluginReload()); await current(reloadRoster()); refreshConfig(); })}>Reload agent roster</button></>}
      </>}
    </div>;

  return <Frame steps={STEPS} at={step} heading={showUpdate || auto ? `Update ${entryShown?.manifest.identity.label ?? selected}` : HEADINGS[step]}
    onBack={step === 1 && !operation && !unknownOperation ? () => { setStep(0); setReview(null); setLocalConfirmed(false); setAdoptConfirmed(false); } : undefined}
    onNext={showUpdate ? update : updateStep ? undefined : step === 0 ? prepare : step === 1 && !operation && !unknownOperation ? install : undefined}
    nextLabel={showUpdate ? `Update to ${versionNext ?? "the new version"}` : step === 0 ? "Review installation" : "Install"}
    nextDisabled={showUpdate ? busy || !entryShown || entryShown.installable === false : step === 0 ? busy || unresolved || !catalog || (mode === "catalog" ? !selected || catalog.catalog.find(c => c.manifest.identity.id === selected)?.installable === false : !local.trim()) : !canInstall}
    dirty={dirty}
    leaveTitle={signinLive ? "Leave sign-in?" : unresolved ? "Leave this operation?" : "Leave setup?"}
    leaveMessage={signinLive ? "Leaving closes this temporary terminal and interrupts sign-in. You can start another attempt from the gallery."
      : unresolved ? "The server operation continues. Return through the gallery to read its status; nothing is enabled automatically."
      : "The installation and vendor data already saved stay on this host. Unsaved fields and confirmations will be lost."}
    enabled={isEnabled} body={body} onCancel={onCancel} />;
}

/** The compact frame for a flow embedded in another surface (the setup wizard's Agents step): the
 *  step chips, the step's heading (focused on every step change), the body, and Close / Back / Next.
 *  No leave guard: closing leaves any server operation running, readable from Settings → Agents. */
function EmbeddedFrame(f: SetupFrame) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const arrived = useRef(false);
  useEffect(() => {
    if (!arrived.current) { arrived.current = true; return; }
    headingRef.current?.focus();
  }, [f.at]);
  return <div className={s.embedded}>
    <ol className={s.chips} aria-label="Setup steps">
      {f.steps.map((st, i) => <li key={st.id} aria-current={i === f.at ? "step" : undefined}
        data-state={i < f.at ? "done" : i === f.at ? "current" : "next"}>{i < f.at && <span aria-hidden="true">✓ </span>}{st.label}</li>)}
    </ol>
    <h3 ref={headingRef} tabIndex={-1} className={s.embeddedHeading}>{f.heading}</h3>
    {f.body}
    <div className={s.row}>
      {f.onCancel && <button type="button" className={button.ghost} onClick={f.onCancel}>{f.enabled ? "Done" : "Close"}</button>}
      {f.onBack && <button type="button" className={button.ghost} onClick={f.onBack}>Back</button>}
      {f.onNext && <button type="button" className={button.primary} disabled={f.nextDisabled} onClick={f.onNext}>{f.nextLabel}</button>}
    </div>
  </div>;
}
