/** Project → Playbook (#2143 P2, #1949): the project's playbook, and every change to it, on the
 *  server flow the New project wizard uses (#1191) — review → confirm → bind → apply → verify, and
 *  remove/plan → remove. Nothing here is a new mechanism: the wizard's hook runs in existing-project
 *  mode (`project_id`, never `create`), and its library, inputs, review and removal pieces are reused.
 *
 *  - **None / removed:** the library; choosing one reviews it, and Apply writes it.
 *  - **Applied:** what is deployed, a verification (run when the tab opens and on Verify again; it
 *    has no stored time), the inputs it reads, and Review update · Verify again · Change playbook… ·
 *    Remove playbook…. Changing is a reviewed REMOVE, then the library — never an in-place swap.
 *  - **Interrupted:** the recorded operation and what each path reached. Retry is offered only
 *    when this page holds that exact operation (its id and its signed review): the server settles
 *    an operation only with the request it began with, and refuses any other while it is pending.
 *  - **Archived:** no deploy action (the server refuses one with 409); verify and remove still work.
 *  A refused write shows the server's words as they came. */
import { ArrowRight, RefreshCw, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Callsign, Panel } from "../../components/hud/Panel";
import { Led, type LedTone } from "../../components/hud/Meters";
import {
  ProjectPlaybookInputs,
  ProjectPlaybookLibrary,
  ProjectPlaybookReview,
  type ProjectPlaybookState,
} from "../../components/playbooks/ProjectPlaybook";
import { ProjectPlaybookRemove } from "../../components/playbooks/ProjectPlaybookRemove";
import type { Manifest } from "../../components/playbooks/playbookDraft";
import { usePlaybookGlobals } from "../../components/playbooks/usePlaybookGlobals";
import { useProjectPlaybook } from "../../components/playbooks/useProjectPlaybook";
import action from "../../components/ui/actionButton.module.css";
import { VARIABLES_PATH } from "../../lib/routes";
import type { ProjectEntity } from "../../types/api";
import type { DeploymentStatus } from "../../types/playbookDeployment";
import type { PlaybookDetail } from "../../types/playbooks";
import { ReadFailed } from "./ProjectBits";
import { usePlaybookDeployment, type FleetRow, type Verification } from "./projectPlaybookData";
import styles from "./projects.module.css";

type Check = Verification["checks"][string] & {
  problems?: Record<string, string>;
  probed?: boolean;
  targets?: unknown[];
  requested?: string[];
};
type Line = { name: string; label: string; tone: LedTone; text: string };

const CHECK_LABELS: Record<string, string> = {
  variables: "Variables",
  materials: "Materials",
  binaries: "Binaries",
  instructions: "Instructions",
  connections: "Connections",
  capabilities: "Capabilities",
};

/** One verify check as a readout. Red only where a check reports a real failure (a missing
 *  binary, a drifted file); a value only a human can give (an input, an agent's instructions) is
 *  amber; a check that does not probe is idle. */
function checkLine(name: string, c: Check): Line {
  const label = CHECK_LABELS[name] ?? name;
  const list = (xs?: string[]) => (xs ?? []).join(", ");
  if (name === "connections") {
    const n = c.targets?.length ?? 0;
    return { name, label, tone: "idle", text: `Not probed — ${n} target${n === 1 ? "" : "s"} listed` };
  }
  if (name === "capabilities") {
    return { name, label, tone: "idle", text: c.requested?.length ? `Requested: ${list(c.requested)}` : "None requested" };
  }
  if (c.ok !== false) {
    const passed: Record<string, string> = {
      variables: "Every input has a value",
      materials: "Managed files match what was applied",
      binaries: "Every required binary found",
      instructions: "Every agent’s instructions present",
    };
    return { name, label, tone: "up", text: passed[name] ?? "Passed" };
  }
  if (name === "variables") {
    const problems = Object.entries(c.problems ?? {}).map(([n, why]) => `${n}: ${why}`);
    return { name, label, tone: "att", text: problems.join(" · ") || "An input needs a value" };
  }
  if (name === "instructions") return { name, label, tone: "att", text: `Missing for ${list(c.missing)}` };
  const parts = [c.error, c.missing?.length ? `missing: ${list(c.missing)}` : "", c.drift?.length ? `drift: ${list(c.drift)}` : ""];
  return { name, label, tone: "down", text: parts.filter(Boolean).join(" · ") || "Failed" };
}

function hhmm(d: Date): string {
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function ProjectPlaybookTab({ entity }: { entity: ProjectEntity }) {
  const data = usePlaybookDeployment(entity.id);
  const destination = entity.default_folder || entity.folders[0] || "";
  const book = useProjectPlaybook(destination, false, entity.id);
  const globals = usePlaybookGlobals(!!book.id);
  // `update`: reviewing a new revision of the deployed playbook. `choose`: the library after a
  // Change playbook… removal. Both end in the same review → apply.
  const [flow, setFlow] = useState<"update" | "choose" | null>(null);
  const [applying, setApplying] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const status = data.status;

  const finish = async (verified: Verification | null, what: string) => {
    await data.reload(verified);
    if (verified) {
      setFlow(null);
      setDone(what);
    }
  };
  const apply = async (what: string) => {
    setApplying(true);
    setDone(null);
    try {
      if (!(await book.prepare())) return;
      await finish(await book.deploy(entity.id), what);
    } finally {
      setApplying(false);
    }
  };
  const retry = async () => {
    setApplying(true);
    setDone(null);
    try {
      await finish(await book.deploy(entity.id), "Applied.");
    } finally {
      setApplying(false);
    }
  };
  const startUpdate = () => {
    if (!status?.playbook_id) return;
    setDone(null);
    setFlow("update");
    book.select(status.playbook_id);
  };
  const removed = (result: DeploymentStatus) => {
    book.removed(result);
    setFlow(null);
    setDone("Removed. The project and its folders stay.");
    void data.reload();
  };

  if (!status) {
    return data.statusError ? (
      <ReadFailed
        title="Couldn’t read the playbook"
        detail={`${data.statusError}. Nothing was changed.`}
        onRetry={() => void data.reload()}
        testId="playbook-read-failed"
      />
    ) : (
      <p className={styles.quiet}>Reading the playbook…</p>
    );
  }

  const none = status.state === "none" || status.state === "removed";
  const busy = applying || removing || book.busy;
  // The server's state outranks the local flow. While an operation is unsettled (interrupted, or
  // bound with this page's request still held) no library and no review is offered: a fresh review
  // or another choice would drop the only request that can resume it, and the server refuses any
  // other operation until that one settles.
  const unresolved = status.state === "interrupted" || (status.state === "bound" && !!book.held);
  const choosing = !unresolved && (none || flow === "choose");
  const updating = !unresolved && flow === "update" && status.state === "applied";
  return (
    <div className={styles.body} data-testid="project-playbook-tab">
      {data.statusError && (
        <p className="nj-error" role="alert">
          Couldn’t refresh the playbook — {data.statusError}. Showing the last state read.
        </p>
      )}
      {applying ? (
        <Applying book={book} />
      ) : choosing ? (
        <ChoosePanel entity={entity} book={book} globals={globals} busy={busy} onApply={() => void apply("Applied and verified.")} />
      ) : (
        <>
          {updating && (
            <UpdatePanel
              book={book}
              globals={globals}
              busy={busy}
              onApply={() => void apply("Updated and verified.")}
              onCancel={() => setFlow(null)}
            />
          )}
          <div className={styles.pbCols}>
            <DeploymentPanel
              entity={entity}
              status={status}
              detail={data.detail}
              detailError={data.detailError}
              fleet={data.fleet}
              verification={data.verification}
              verifiedAt={data.verifiedAt}
              verifyError={data.verifyError}
              verifying={data.verifying}
              onVerify={() => void data.verify()}
              book={book}
              busy={busy}
              updating={updating}
              onUpdate={startUpdate}
              onRetry={() => void retry()}
              onRemoving={setRemoving}
              onRemoved={removed}
              onChanged={(result) => {
                book.removed(result);
                setDone(null);
                setFlow("choose");
                void data.reload();
              }}
              onRemoveFailed={() => void data.reload()}
            />
            <InputsPanel detail={data.detail} verification={data.verification} fleet={data.fleet} />
          </div>
        </>
      )}
      {book.error && !applying && (
        <p className="nj-error" role="alert" data-testid="playbook-error">
          {book.error}
        </p>
      )}
      {done && !book.error && (
        <p className={styles.quiet} role="status" data-testid="playbook-done">
          {done}
        </p>
      )}
    </div>
  );
}

/** Review → bind → apply → verify, one stage at a time; every control waits until it settles. */
function Applying({ book }: { book: ProjectPlaybookState }) {
  const order = ["review", "bind", "apply", "verify"] as const;
  const at = book.stage === "idle" ? 0 : book.stage === "done" ? 4 : order.indexOf(book.stage);
  const words = ["Reviewed", "Inputs bound", "Applying files", "Verify"];
  return (
    <Panel tone="quiet" className={styles.panel} aria-labelledby="pb-applying-h">
      <Callsign as="h2" id="pb-applying-h" title="Playbook assignment" value={`${words[Math.min(at, 3)]}…`} />
      <ol className={styles.stages} aria-label="Stages" data-testid="playbook-stages">
        {words.map((w, i) => (
          <li key={w} className={styles.ledWord} aria-current={i === at ? "step" : undefined}>
            <Led tone={i < at ? "up" : i === at ? "att" : "idle"} />
            {w}
          </li>
        ))}
      </ol>
      <p className={styles.quiet} role="status">
        {at <= 1 ? "Binding the reviewed inputs…" : at === 2 ? "Applying files…" : "Verifying…"}
      </p>
    </Panel>
  );
}

function ChoosePanel({
  entity,
  book,
  globals,
  busy,
  onApply,
}: {
  entity: ProjectEntity;
  book: ProjectPlaybookState;
  globals: ReturnType<typeof usePlaybookGlobals>;
  busy: boolean;
  onApply: () => void;
}) {
  return (
    <Panel tone="quiet" className={styles.panel} aria-labelledby="pb-h">
      <Callsign as="h2" id="pb-h" title="Playbook" value="none" />
      {entity.archived ? (
        <p className={styles.text} data-testid="playbook-archived">
          {entity.name} is archived, so it can’t take a playbook. Unarchive it on the General tab first.
        </p>
      ) : (
        <>
          <ProjectPlaybookLibrary book={book} noneHint="Leave this project without a playbook." />
          {book.id && book.detail && (
            <>
              <ProjectPlaybookInputs book={book} globals={globals} />
              <ProjectPlaybookReview book={book} />
              <div className={styles.actions}>
                <button
                  type="button"
                  className={action.primary}
                  disabled={busy || !book.reviewed || !book.canCreate}
                  onClick={onApply}
                >
                  Apply reviewed changes to this project
                </button>
              </div>
            </>
          )}
        </>
      )}
    </Panel>
  );
}

function UpdatePanel({
  book,
  globals,
  busy,
  onApply,
  onCancel,
}: {
  book: ProjectPlaybookState;
  globals: ReturnType<typeof usePlaybookGlobals>;
  busy: boolean;
  onApply: () => void;
  onCancel: () => void;
}) {
  return (
    <Panel tone="quiet" className={styles.panel} aria-labelledby="pb-update-h">
      <Callsign as="h2" id="pb-update-h" title="Playbook update" value={book.detail?.name || book.id || "…"} />
      {book.loading && <p className={styles.quiet}>Reading the playbook’s current revision…</p>}
      {book.loadError && (
        <p className="nj-error" role="alert">
          {book.loadError}
        </p>
      )}
      {book.detail && (
        <>
          {book.detail.source === "local" && (
            <label className={styles.trust}>
              <input type="checkbox" checked={book.trust} onChange={(e) => book.setTrust(e.target.checked)} />
              I have reviewed this unsigned local playbook and trust its instructions and proposed files.
            </label>
          )}
          <ProjectPlaybookInputs book={book} globals={globals} />
          <ProjectPlaybookReview book={book} />
        </>
      )}
      <div className={styles.actions}>
        <button
          type="button"
          className={action.primary}
          disabled={busy || !book.reviewed || !book.canCreate}
          onClick={onApply}
        >
          Apply reviewed changes to this project
        </button>
        <button type="button" className={action.ghost} disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </Panel>
  );
}

function DeploymentPanel({
  entity,
  status,
  detail,
  detailError,
  fleet,
  verification,
  verifiedAt,
  verifyError,
  verifying,
  onVerify,
  book,
  busy,
  updating,
  onUpdate,
  onRetry,
  onRemoving,
  onRemoved,
  onChanged,
  onRemoveFailed,
}: {
  entity: ProjectEntity;
  status: DeploymentStatus;
  detail: PlaybookDetail | null;
  detailError: string | null;
  fleet: FleetRow | null;
  verification: Verification | null;
  verifiedAt: Date | null;
  verifyError: string | null;
  verifying: boolean;
  onVerify: () => void;
  book: ProjectPlaybookState;
  busy: boolean;
  updating: boolean;
  onUpdate: () => void;
  onRetry: () => void;
  onRemoving: (busy: boolean) => void;
  onRemoved: (status: DeploymentStatus) => void;
  onChanged: (status: DeploymentStatus) => void;
  onRemoveFailed: () => void;
}) {
  const applied = status.state === "applied";
  const interrupted = status.state === "interrupted";
  const op = interrupted ? status.operation : undefined;
  // Retry only with the operation the server recorded AND this page holds (its signed review).
  const heldOp = op === "apply" ? book.held?.apply : op === "bind" ? book.held?.bind : undefined;
  const canRetry =
    (interrupted && !!status.operation_id && heldOp === status.operation_id) ||
    (status.state === "bound" && !!book.held);
  const update = !!fleet?.updateAvailable && !entity.archived && applied;
  const name = detail?.name || status.playbook_id || "Playbook";
  const removable = op !== "bind";
  const stamp = fleet?.updateAvailable
    ? `Update available${detail?.version ? ` → v${detail.version}` : ""}`
    : detail?.version
      ? `v${detail.version}`
      : null;
  return (
    <Panel tone="quiet" className={styles.panel} aria-labelledby="pb-h">
      <Callsign
        as="h2"
        id="pb-h"
        title="Playbook"
        value={interrupted ? "interrupted" : status.state === "bound" ? "bound — not applied" : status.state}
        meta={fleet?.revision ? `rev ${fleet.revision.slice(0, 7)}` : undefined}
      />
      <div className={styles.pbHead}>
        <h3 className={styles.pbName} data-testid="playbook-name">
          {name}
        </h3>
        {stamp && (
          <span className={`nj-stamp ${fleet?.updateAvailable ? styles.stampAccent : ""}`} data-testid="playbook-stamp">
            {stamp}
          </span>
        )}
      </div>
      {detailError && <p className={styles.quietLine}>{detailError}. The deployment itself is shown from its record.</p>}
      {detail && (
        <dl className={styles.facts}>
          <div>
            <dt>Source</dt>
            <dd>Library · {detail.source} playbook</dd>
          </div>
          {!!detail.flows?.length && (
            <div>
              <dt>Flow</dt>
              <dd>{detail.flows[0].steps.map((s) => s.title).join(" → ")}</dd>
            </div>
          )}
        </dl>
      )}
      {interrupted && <Interrupted status={status} canRetry={canRetry} />}
      {applied && (
        <VerifyBlock verification={verification} verifiedAt={verifiedAt} error={verifyError} verifying={verifying} />
      )}
      {entity.archived && (
        <p className={styles.quietLine} data-testid="playbook-archived">
          {entity.name} is archived: it can’t take an update or another playbook. Verify and Remove still work.
        </p>
      )}
      <div className={styles.pbActions}>
        {canRetry && (
          <button type="button" className={action.primary} disabled={busy} onClick={onRetry} data-testid="playbook-retry">
            <RefreshCw size={14} aria-hidden="true" /> Retry {op === "bind" ? "binding" : "apply"}
          </button>
        )}
        {update && !updating && (
          <button type="button" className={action.primary} disabled={busy} onClick={onUpdate}>
            <RefreshCw size={14} aria-hidden="true" /> Review update
          </button>
        )}
        {applied && (
          <button
            type="button"
            className={verification && !verification.ok && !update ? action.primary : action.ghost}
            disabled={busy || verifying}
            onClick={onVerify}
          >
            <ShieldCheck size={14} aria-hidden="true" /> {verifying ? "Verifying…" : "Verify again"}
          </button>
        )}
        {/* Both removals stay mounted while a deployment exists: the one that started an
            operation holds its id and reviewed digest, and only it can resume it. */}
        {removable && !entity.archived && (
          <ProjectPlaybookRemove
            project={entity.id}
            disabled={busy}
            onBusy={onRemoving}
            onRemoved={onChanged}
            onFailed={onRemoveFailed}
            label="Change playbook…"
            note="Changing a playbook removes this one first, through its reviewed removal. Once it is removed, choose the next one here."
            testId="playbook-change"
          />
        )}
        {removable && (
          <span className={styles.pushEnd}>
            <ProjectPlaybookRemove
              project={entity.id}
              disabled={busy}
              onBusy={onRemoving}
              onRemoved={onRemoved}
              onFailed={onRemoveFailed}
              label="Remove playbook…"
              buttonClassName={action.danger}
              testId="playbook-remove"
            />
          </span>
        )}
      </div>
      {applied && !book.error && (
        <p className={styles.quietLine}>
          Review shows every file and binding a change would make before anything is written; nothing is applied until
          you confirm. Remove reviews first too.
        </p>
      )}
    </Panel>
  );
}

function Interrupted({ status, canRetry }: { status: DeploymentStatus; canRetry: boolean }) {
  const what = status.operation === "remove" ? "removal" : status.operation === "bind" ? "binding" : "apply";
  return (
    <div className={styles.verify} data-tone="down" data-testid="playbook-interrupted">
      <p className={styles.verifyHead}>
        <Led tone="down" />
        <b>The {what} was interrupted</b>
        <span>
          Operation <code>{status.operation_id ?? "unrecorded"}</code>
        </span>
      </p>
      {!!status.files?.length && (
        <ul className={styles.checks} aria-label="What each path reached">
          {status.files.map((f) => (
            <li key={f.path}>
              <code>{f.path}</code>
              <span>{f.state}</span>
            </li>
          ))}
        </ul>
      )}
      <p className={styles.quietLine}>
        {canRetry
          ? "Retry resumes this same operation with the review it started from; it never starts a second one."
          : status.operation === "remove"
            ? "Only the removal dialog that started it can resume it: the server settles an operation only with the reviewed plan it began with, and refuses any other while this one is pending."
            : status.operation === "apply"
              ? "This page doesn’t hold the signed review this apply started from (it is never stored), so it can’t resume it. Remove playbook… settles it: removal takes back only the files this apply proves it wrote."
              : "This page doesn’t hold the inputs this binding started from (they are never stored), so it can’t resume it."}
      </p>
    </div>
  );
}

function VerifyBlock({
  verification,
  verifiedAt,
  error,
  verifying,
}: {
  verification: Verification | null;
  verifiedAt: Date | null;
  error: string | null;
  verifying: boolean;
}) {
  if (error)
    return (
      <p className="nj-error" role="alert" data-testid="playbook-verify-error">
        {error}. Verify again to retry; nothing was changed.
      </p>
    );
  if (!verification) return <p className={styles.quiet}>{verifying ? "Verifying…" : "Not verified yet."}</p>;
  const lines = Object.entries(verification.checks).map(([n, c]) => checkLine(n, c as Check));
  const passed = lines.filter((l) => l.tone === "up").length;
  const failing = lines.filter((l) => l.tone === "down").length;
  const attention = lines.filter((l) => l.tone === "att").length;
  const idle = lines.filter((l) => l.tone === "idle").length;
  const tone: LedTone = failing ? "down" : attention ? "att" : "up";
  const title = failing || attention ? "Needs attention" : "Verified";
  const tally = [
    `${passed} passed`,
    failing && `${failing} failed`,
    attention && `${attention} need${attention === 1 ? "s" : ""} attention`,
    idle && `${idle} not probed`,
  ].filter(Boolean);
  return (
    <div className={styles.verify} data-tone={tone} data-testid="playbook-verify">
      <p className={styles.verifyHead}>
        <Led tone={tone} />
        <b>{title}</b>
        <span>
          Verified {verifiedAt ? `at ${hhmm(verifiedAt)}` : ""} · {tally.join(" · ")}
        </span>
      </p>
      <ul className={styles.checks} aria-label="Checks">
        {lines.map((l) => (
          <li key={l.name} data-tone={l.tone}>
            <span className={styles.ledWord}>
              <Led tone={l.tone} />
              {l.label}
            </span>
            <span>{l.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The inputs, and what the last verification said of each. Verify checks the DEPLOYED revision's
 *  inputs, while the declarations come from the playbook's current revision: a verdict is drawn as
 *  Filled only when the two are the same revision. Otherwise an input verify did not name is "not
 *  verified", and an input verify still reports a problem for is listed even when the current
 *  revision no longer declares it. */
function InputsPanel({
  detail,
  verification,
  fleet,
}: {
  detail: PlaybookDetail | null;
  verification: Verification | null;
  fleet: FleetRow | null;
}) {
  const manifest = detail?.documents?.["playbook.toml"] as Manifest | undefined;
  const declared = (manifest?.variables ?? []).map((v) => ({
    name: v.name,
    kind: v.kind === "secret" ? "secret" : (v.type ?? "text"),
  }));
  const problems = (verification?.checks.variables as Check | undefined)?.problems ?? {};
  const known = !!verification?.checks.variables;
  const same = !!fleet && !fleet.updateAvailable && !!detail && fleet.revision === detail.revision;
  // A deployed input verify still fails on, which the current revision no longer declares.
  const gone = Object.keys(problems)
    .filter((n) => !declared.some((d) => d.name === n))
    .map((name) => ({ name, kind: "deployed" }));
  const rows = [...declared, ...gone];
  const filled = declared.filter((v) => !problems[v.name]).length;
  return (
    <Panel tone="quiet" className={styles.panel} aria-labelledby="pb-inputs-h">
      <Callsign
        as="h2"
        id="pb-inputs-h"
        title="Inputs"
        value={rows.length ? (known && same ? `${filled} of ${declared.length} filled` : `${rows.length}`) : "none"}
      />
      {known && !same && rows.length > 0 && (
        <p className={styles.quietLine} data-testid="playbook-inputs-revision">
          Listed from the playbook’s current revision. Verify checks the deployed one, so only the problems it
          reports are certain until the update is applied.
        </p>
      )}
      {!rows.length ? (
        <p className={styles.quiet}>{detail ? "This playbook declares no inputs." : "—"}</p>
      ) : (
        <ul className={styles.rows} aria-label="Inputs">
          {rows.map((v) => {
            const problem = problems[v.name];
            const word = problem ? "Waiting" : known && same ? "Filled" : known ? "Not verified" : null;
            const tone: LedTone = problem ? "att" : known && same ? "up" : "idle";
            return (
              <li key={v.name} data-testid={`playbook-input-${v.name}`}>
                <span className={styles.rowMain}>
                  <b>
                    {v.name} <span className="nj-stamp">{v.kind}</span>
                  </b>
                  {problem && <span>{problem}</span>}
                </span>
                {word && (
                  <span className={styles.ledWord}>
                    <Led tone={tone} />
                    {word}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      <p className={styles.quietLine}>
        Inputs are bound when the playbook is reviewed. To change one, review the update (or change the playbook) and
        bind it again.
      </p>
      <Link to={VARIABLES_PATH} className={styles.link}>
        Manage values in Library → Variables <ArrowRight size={13} aria-hidden="true" />
      </Link>
    </Panel>
  );
}
