/** The templates that decide what "done" means, editable at last (#892, Phase 3b of #840).
 *
 * `prefs.mission_playbooks` shipped with #883 — validated, defaulted, consumed by every mission
 * that gets objectives — and with no surface whatsoever. The only way to change what a mission
 * checks was to hand-edit `prefs.json` on the server, which is not a feature so much as an
 * admission. This is that surface.
 *
 * **A playbook objective is the ONLY place a probe target can be born (#883).** The model
 * proposes objectives by INDEX into these templates and can never author a `probe` or a
 * `probe_args`; the runner reads its target from the stored row and from operator config, never
 * from anything a model produced. So a human typing here is the entire supply of probe targets,
 * and this editor is that authority model's user interface — which is why it does not try to be
 * clever about what it accepts.
 *
 * **The whole block saves at once**, because that is how the server takes it:
 * `set_mission_playbooks` validates and replaces the block, refusing the write entirely on a bad
 * row rather than dropping it. Per-field autosave would fight that contract and could half-apply
 * an edit the server considers indivisible. The refusal is shown VERBATIM — it names the id, the
 * key and the reason, and paraphrasing it would throw away the only thing that says what to fix.
 */
import { useEffect, useRef, useState } from "react";

import { useConfig, useConfigRefresh } from "../app/config";
import { api, ApiError } from "../lib/api";
import type {
  MissionPlaybook,
  MissionPlaybookObjective,
  MissionPlaybooks as Block,
  MissionProbeSchema,
} from "../types/api";

import { DirectionField } from "../components/pulse/DirectionField";
import styles from "./Settings.module.css";

const EMPTY: Block = { default_id: "", playbooks: [], revision: 0 };

/** Under a template's Direction field (#983 P2, D1). */
const DIRECTION_HINT =
  "Written by you. Mission control decides when to send it; the AI cannot change a word. Facts come from this objective's own check, never from what the session printed. Leave it empty to send your default nudge.";

/** Mirrors `prefs._PLAYBOOK_ID_RE`. Used only to explain a refusal BEFORE the round trip; the
 *  server checks it again and its answer is the one that counts. */
const ID_RE = /^[a-z0-9][a-z0-9_-]*$/;

/** A row identity that never leaves the client. Monotonic rather than random because it only has
 *  to be unique within one mounted panel, and a counter is testable. */
let nextRid = 1;
function newRid(): string {
  return `row_${nextRid++}`;
}

function blankObjective(n: number): MissionPlaybookObjective {
  return {
    key: `objective_${n}`,
    title: "",
    probe: "none",
    probe_args: null,
    gate: false,
  };
}

export function MissionPlaybooks() {
  const cfg = useConfig();
  const refreshConfig = useConfigRefresh();
  const stored = cfg?.mission_playbooks;
  const schema: MissionProbeSchema | undefined = cfg?.mission_probes;

  const [block, setBlock] = useState<Block>(stored ?? EMPTY);

  // AN UNSAVED DRAFT WINS OVER ANYTHING THAT ARRIVES LATER (#900 review, finding 7).
  //
  // Two mechanisms, because there are two ways a stale copy lands on top of a newer edit: the
  // save's own response, and the config refresh it triggers. Both are round trips and both are
  // slower than typing, and either one replacing the block silently discards work the operator
  // watched themselves do.
  //
  // `revision` is a ref because the save's closure — created before the edit happened — has to
  // read the value as it is at RESOLUTION time; `dirty` is state because the re-seed below is
  // render-time, where a ref may not be read.
  const revision = useRef(0);
  const [dirty, setDirty] = useState(false);

  /** A STABLE CLIENT IDENTITY PER ROW, minted here and never sent (#900 review 4, finding 3).
   *
   *  Provenance used to be a set of INDEXES, and an index is not an identity: it is a position
   *  that shifts under an insert, a removal, or — the case that broke — a save that resolves
   *  while the operator adds a row. `rids[i]` is what the row IS, for as long as it is on screen.
   *
   *  Kept beside `block.playbooks` rather than on the rows themselves, because the block is the
   *  request body and `POST /api/prefs` is an allowlist: a field the client invented would be a
   *  422, and stripping it before every send is one more thing to forget. */
  const [rids, setRids] = useState<string[]>(() =>
    (stored ?? EMPTY).playbooks.map(() => newRid()),
  );

  /** WHICH ROWS THIS PANEL CREATED, by `rid`. A row's id is editable only while it has never
   *  been saved — the id is what missions store as `playbook_id`, so renaming one after use
   *  would orphan every mission naming it. Provenance is the honest test for that; comparing the
   *  draft id against the stored ids is not, because typing an id that already exists then locks
   *  the field the operator is trying to correct (#900 review 2, finding 4). */
  const [draftRows, setDraftRows] = useState<Set<string>>(new Set());

  /** The rows and their ids AS THEY ARE NOW, for a save that resolves later (#900 review 7,
   *  finding 7). Published through an effect rather than assigned during render — the same
   *  stable-ref idiom the terminal uses for `refitSoon` — because a callback that settles
   *  provenance has to ask what the row is at RESOLUTION time, not what it was when the request
   *  was built. */
  const ridsRef = useRef(rids);
  const blockRef = useRef(block);
  useEffect(() => {
    ridsRef.current = rids;
    blockRef.current = block;
  }, [rids, block]);

  // Re-seed when the shared config changes underneath (another tab saved, or the panel
  // remounted after a refresh) — the same idiom `PulseSettings` uses, fenced on the draft.
  const [synced, setSynced] = useState(stored);
  if (stored !== synced) {
    setSynced(stored);
    if (stored && !dirty) {
      setBlock(stored);
      // A block that arrived from the server has no drafts in it, so every row is stored and
      // gets a fresh identity. Doing this here keeps `rids` and `block.playbooks` the same
      // length on every path that replaces the array wholesale.
      setRids(stored.playbooks.map(() => newRid()));
      setDraftRows(new Set());
    }
  }

  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  const edit = (fn: (b: Block) => Block) => {
    revision.current += 1;
    setDirty(true);
    setSaved(false);
    setBlock(fn);
  };

  const editPlaybook = (
    i: number,
    fn: (p: MissionPlaybook) => MissionPlaybook,
  ) =>
    edit((b) => ({
      ...b,
      playbooks: b.playbooks.map((p, j) => (j === i ? fn(p) : p)),
    }));

  const editObjective = (
    i: number,
    j: number,
    fn: (o: MissionPlaybookObjective) => MissionPlaybookObjective,
  ) =>
    editPlaybook(i, (p) => ({
      ...p,
      objectives: p.objectives.map((o, k) => (k === j ? fn(o) : o)),
    }));

  const save = async () => {
    setError(null);
    setBusy(true);
    // THE REVISION THE REQUEST IS CARRYING. The response replaces the whole block with the
    // server's normalized copy, which is right — and destroys any edit made while it was in
    // flight, which is not (#900 review, finding 7). Typing during a save is not an exotic
    // sequence: the button is at the bottom of a long form and the request is a round trip.
    //
    // A revision, not a deep compare: the point is "did the operator change anything since",
    // and `edit()` bumps it on every keystroke, so the answer is exact and cheap.
    const sent = revision.current;
    // WHICH DRAFT ROWS THIS REQUEST IS CARRYING. The response can only settle the provenance of
    // rows that were in it: a row added while it is in flight was never sent, never accepted, and
    // must stay editable (#900 review 4, finding 3). Clearing the whole set froze that row's id
    // the moment an earlier save landed — an identity missions could name, that the server had
    // never seen.
    // …AND UNDER WHICH IDENTITY (#900 review 7, finding 7). The row id is stable; the PLAYBOOK
    // id in it is not, and that is the one the server accepted. Submit `foo`, rename it to `bar`
    // while the request is in flight, and settling on the row alone froze `bar` as immutable
    // although only `foo` exists server-side — an identity missions could name that had never
    // been saved, and the operator could not edit their way out of it.
    const sentDraftIds = new Map(
      rids
        .map((rid, i) => [rid, block.playbooks[i]?.id ?? ""] as const)
        .filter(([rid]) => draftRows.has(rid)),
    );
    try {
      // THE REVISION THIS EDIT WAS MADE AGAINST rides with the block (#900 review 5, finding 7).
      // A whole-block write with no comparand is last-writer-wins over everything another tab
      // did — a playbook it added, a probe target it fixed, a gate it set — deleted silently,
      // with both operators told the save worked.
      const r = (await api.setPrefs({ mission_playbooks: block })) as {
        mission_playbooks?: Block;
      };
      // Only when nothing moved under us. Otherwise the save still SUCCEEDED — it is the
      // server's answer about the block that was sent — but the newer draft is what the
      // operator is looking at, so it stays, and `saved` is not claimed for a block that is no
      // longer on screen.
      // PROVENANCE SETTLES ON THE SUBMITTED SNAPSHOT, not on the block that is on screen (#900
      // review 3, finding 3). The server ACCEPTED these ids, so they are identities missions can
      // name from this moment — whether or not the operator has typed something since. Keeping
      // that inside the revision fence left a persisted id editable after an in-flight save,
      // and it could then be renamed out from under a mission that already referenced it.
      if (r.mission_playbooks)
        setDraftRows((s) => {
          const next = new Set(s);
          for (const [rid, sentId] of sentDraftIds) {
            // Only where the identity is still the one that was accepted. A row renamed since is
            // a row the server has never seen under that name, so it stays a draft.
            const i = ridsRef.current.indexOf(rid);
            if (i >= 0 && blockRef.current.playbooks[i]?.id === sentId)
              next.delete(rid);
          }
          return next;
        });
      // …while the BLOCK still respects the fence: a newer draft is what the operator is looking
      // at, and the server's normalized copy must not replace it. Two different questions, and
      // conflating them is what produced the defect.
      if (r.mission_playbooks && revision.current === sent) {
        setBlock(r.mission_playbooks);
        setDirty(false);
        setSaved(true);
      } else if (r.mission_playbooks) {
        // THE REVISION STILL MOVES (#900 review 7, finding 6). The save SUCCEEDED — the server
        // is now at N+1 — and keeping the on-screen block at N meant the operator's next save
        // was refused with a 409 whose only offered remedy is a reload, which throws away the
        // very edit this fence was protecting. The block the operator is looking at stays;
        // only the version it was made against catches up.
        const accepted = r.mission_playbooks.revision;
        setBlock((b) => ({ ...b, revision: accepted }));
      }
      refreshConfig();
    } catch (e) {
      // VERBATIM. `set_mission_playbooks` answers with the id, the key and the reason —
      // "bad objective key 'Checks Green'", "playbook 'ship' has duplicate objective keys" — and
      // a friendlier sentence would be a less useful one.
      setError(
        e instanceof ApiError && e.status === 409
          ? `${e.message} Nothing was overwritten — reload to see the current playbooks.`
          : e instanceof ApiError && e.message
            ? e.message
            : "The playbooks could not be saved.",
      );
    } finally {
      setBusy(false);
    }
  };

  const kinds = schema?.kinds ?? ["none"];
  const nonGating = new Set(schema?.non_gating ?? []);

  return (
    <section className={styles.section} data-testid="mission-playbooks">
      <h2>Mission playbooks</h2>
      <p className={styles.blurb}>
        A playbook is the checklist a new mission starts with — what has to be
        true before the work counts as done. Objectives with a <b>probe</b> are
        checked by the server; the rest you settle yourself. Only a playbook can
        give an objective something to check, which is what keeps an AI from
        pointing one somewhere nobody chose.
      </p>

      <label className={styles.aiField}>
        <span className={styles.aiFieldLabel}>Default playbook</span>
        <select
          className={styles.aiInput}
          value={block.default_id}
          onChange={(e) => edit((b) => ({ ...b, default_id: e.target.value }))}
          data-testid="playbook-default"
        >
          {/* NOT "the first one". A mission with no playbook gets notes-only objectives, which
              is a different and safer thing than inheriting somebody else's gates. */}
          <option value="">None — new missions start with no checklist</option>
          {block.playbooks.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label || p.id}
            </option>
          ))}
        </select>
      </label>

      {block.playbooks.map((p, i) => {
        // NEW-NESS IS A FACT ABOUT THE ROW, NOT ABOUT ITS CURRENT TEXT (#900 review 2, finding 4).
        //
        // Inferring it by comparing the draft id to the stored ids made the field lock itself the
        // moment the operator typed an id that already existed: the input became immutable
        // `<code>`, Save then refused the duplicate, and the only way out was to delete the whole
        // row and start again. `draftRows` records which rows this panel created, so the id stays
        // editable until the row has actually been saved — which is the thing that makes it an
        // identity missions can name.
        const isNew = draftRows.has(rids[i]);
        return (
          <div key={i} className={styles.playbook} data-testid="playbook">
            <div className={styles.playbookHead}>
              {/* The id is the IDENTITY: missions store `playbook_id`, so renaming one after it
                  has been used would orphan every mission that names it. Editable while the
                  playbook is new and static afterwards. */}
              {isNew ? (
                <input
                  className={styles.aiInput}
                  value={p.id}
                  placeholder="id (lowercase, no spaces)"
                  aria-label="Playbook id"
                  onChange={(e) =>
                    editPlaybook(i, (pb) => ({ ...pb, id: e.target.value }))
                  }
                  data-testid="playbook-id"
                />
              ) : (
                <code className={styles.playbookId}>{p.id}</code>
              )}
              <input
                className={styles.aiInput}
                value={p.label}
                placeholder="What this playbook is for"
                aria-label="Playbook label"
                onChange={(e) =>
                  editPlaybook(i, (pb) => ({ ...pb, label: e.target.value }))
                }
                data-testid="playbook-label"
              />
              <button
                type="button"
                className={styles.secBtnGhost}
                onClick={() => {
                  edit((b) => {
                    const playbooks = b.playbooks.filter((_, j) => j !== i);
                    return {
                      ...b,
                      // A removed playbook must not stay the default — the server refuses a
                      // `default_id` naming no playbook, and silently keeping it would make
                      // every later save fail for a reason nothing on screen explains.
                      //
                      // ASKED OF WHAT REMAINS, not of the row being deleted (#900 review 7,
                      // finding 10). With a stored `ship` and a draft the operator had also
                      // typed `ship` into, removing the DRAFT cleared the default — unsetting
                      // the real playbook's role because a row that never existed shared its
                      // name.
                      default_id: playbooks.some((q) => q.id === b.default_id)
                        ? b.default_id
                        : "",
                      playbooks,
                    };
                  });
                  // NO REMAPPING. The identities are the rows', not the positions', so removing
                  // one leaves the others' provenance exactly as it was — which is the whole
                  // reason they are not indices any more.
                  const gone = rids[i];
                  setRids((rs) => rs.filter((_, j) => j !== i));
                  setDraftRows((s) => {
                    const next = new Set(s);
                    next.delete(gone);
                    return next;
                  });
                }}
                data-testid="playbook-remove"
              >
                REMOVE
              </button>
            </div>

            {p.objectives.map((o, j) => {
              const spec = schema?.args[o.probe];
              const names = [
                ...(spec?.required ?? []),
                ...(spec?.optional ?? []),
              ];
              const args = (o.probe_args ?? {}) as Record<string, unknown>;
              const types = schema?.types?.[o.probe] ?? {};
              return (
                <div
                  key={j}
                  className={styles.objective}
                  data-testid="playbook-objective"
                >
                  <input
                    className={styles.aiInput}
                    value={o.key}
                    placeholder="key"
                    aria-label="Objective key"
                    onChange={(e) =>
                      editObjective(i, j, (ob) => ({
                        ...ob,
                        key: e.target.value,
                      }))
                    }
                    data-testid="objective-key"
                  />
                  <input
                    className={styles.aiInput}
                    value={o.title}
                    placeholder="What has to be true"
                    aria-label="Objective title"
                    onChange={(e) =>
                      editObjective(i, j, (ob) => ({
                        ...ob,
                        title: e.target.value,
                      }))
                    }
                    data-testid="objective-title"
                  />
                  <select
                    className={styles.aiInput}
                    value={o.probe}
                    aria-label="How it is checked"
                    onChange={(e) => {
                      const probe = e.target.value;
                      editObjective(i, j, (ob) => ({
                        ...ob,
                        probe,
                        // The arguments belong to the KIND. Carrying the old ones over would
                        // send `url` to `forge_pr`, which the server rejects as an unknown
                        // argument — correctly, and confusingly.
                        probe_args: null,
                        gate: nonGating.has(probe) ? false : ob.gate,
                      }));
                    }}
                    data-testid="objective-probe"
                  >
                    {kinds.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                  {names.map((name) => (
                    <input
                      key={name}
                      className={styles.aiInput}
                      value={String(args[name] ?? "")}
                      // THE ARGUMENT'S OWN TYPE, from the server's schema. Every input yields a
                      // string, and `http_status.expect_status` strictly requires an integer —
                      // so before this the editor offered a field the server could only ever
                      // refuse (#900 review, finding 6). `inputMode` also gets the numeric
                      // keypad on the phone, which is the surface this panel is used from.
                      inputMode={types[name] === "int" ? "numeric" : undefined}
                      placeholder={
                        spec?.required.includes(name)
                          ? `${name} (required)`
                          : name
                      }
                      aria-label={`${o.probe} ${name}`}
                      onChange={(e) =>
                        editObjective(i, j, (ob) => {
                          const next = {
                            ...((ob.probe_args ?? {}) as Record<
                              string,
                              unknown
                            >),
                          };
                          const raw = e.target.value;
                          // An EMPTY field is an absent argument, not an empty string: the
                          // server refuses a blank where it wants text, and an operator who
                          // typed and then cleared a value means "I do not want this one".
                          if (!raw) delete next[name];
                          else if (types[name] === "int") {
                            // Digits only, and the RAW STRING is kept when it is not a clean
                            // integer — never `NaN`, never a silent coercion. A half-typed "2"
                            // has to survive to "204", and a value the server would refuse must
                            // reach it so the refusal names the field rather than the editor
                            // quietly dropping it.
                            next[name] = /^\d+$/.test(raw) ? Number(raw) : raw;
                          } else next[name] = raw;
                          return {
                            ...ob,
                            probe_args: Object.keys(next).length ? next : null,
                          };
                        })
                      }
                      data-testid={`objective-arg-${name}`}
                    />
                  ))}
                  <label
                    className={styles.aiToggle}
                    data-testid="objective-gate-label"
                  >
                    <input
                      type="checkbox"
                      checked={o.gate}
                      // A probe that cannot be evidence may not gate alone, so the control is
                      // withdrawn rather than offered and then refused.
                      disabled={nonGating.has(o.probe)}
                      onChange={(e) =>
                        editObjective(i, j, (ob) => ({
                          ...ob,
                          gate: e.target.checked,
                        }))
                      }
                      data-testid="objective-gate"
                    />
                    <span>Required for done</span>
                  </label>
                  <button
                    type="button"
                    className={styles.secBtnGhost}
                    onClick={() =>
                      editPlaybook(i, (pb) => ({
                        ...pb,
                        objectives: pb.objectives.filter((_, k) => k !== j),
                      }))
                    }
                    data-testid="objective-remove"
                  >
                    DROP
                  </button>
                  {/* THE DIRECTION (#983 P2). Saved with the block like every other field, and
                      validated by the same save: an unknown placeholder refuses the whole write,
                      and the preview says so first, in the same words. An emptied field is an
                      absent direction, not an empty string. */}
                  <DirectionField
                    value={o.direction ?? ""}
                    onChange={(text) =>
                      editObjective(i, j, (ob) => {
                        const next = { ...ob };
                        if (text) next.direction = text;
                        else delete next.direction;
                        return next;
                      })
                    }
                    probe={o.probe}
                    placeholders={schema?.placeholders}
                    label="typed into the session when mission control nudges about this objective"
                    hint={DIRECTION_HINT}
                    testId="objective-direction"
                  />
                </div>
              );
            })}

            <button
              type="button"
              className={styles.secBtnGhost}
              onClick={() =>
                editPlaybook(i, (pb) => ({
                  ...pb,
                  objectives: [
                    ...pb.objectives,
                    blankObjective(pb.objectives.length + 1),
                  ],
                }))
              }
              data-testid="objective-add"
            >
              ADD OBJECTIVE
            </button>
          </div>
        );
      })}

      <div className={styles.aiActions}>
        <button
          type="button"
          className={styles.secBtnGhost}
          onClick={() => {
            edit((b) => ({
              ...b,
              playbooks: [
                ...b.playbooks,
                { id: "", label: "", objectives: [blankObjective(1)] },
              ],
            }));
            const rid = newRid();
            setRids((rs) => [...rs, rid]);
            setDraftRows((s) => new Set(s).add(rid));
          }}
          data-testid="playbook-add"
        >
          ADD PLAYBOOK
        </button>
        <button
          type="button"
          className={styles.secBtn}
          disabled={busy}
          onClick={() => void save()}
          data-testid="playbook-save"
        >
          {busy ? "Saving…" : "Save playbooks"}
        </button>
        {saved ? <span className={styles.ok}>Saved.</span> : null}
      </div>

      {block.playbooks.some((p) => p.id && !ID_RE.test(p.id)) ? (
        <p className={styles.warn}>
          A playbook id is lowercase letters, digits, <code>-</code> and{" "}
          <code>_</code>, starting with a letter or digit.
        </p>
      ) : null}
      {error ? (
        <p className={styles.err} role="alert" data-testid="playbook-error">
          {error}
        </p>
      ) : null}
    </section>
  );
}
