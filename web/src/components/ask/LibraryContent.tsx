/** What a Library card saves (#1611), shown verbatim: a playbook's or skill's files, the diff an
 *  edit makes against the version HQ read, or an automation's scope as the server renders it.
 *
 *  The content is fetched once from the card's own route, never polled with the conversation, and
 *  the server serves it only while the draft still matches what the card is bound to. Everything
 *  is TEXT: a file or an instruction is never rendered as markup. A long file is collapsed after
 *  `PREVIEW_LINES` with a "Show all" control; nothing is ever withheld before the operator taps
 *  Confirm, it is only folded. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../../lib/api";
import type { AskLibraryContent, AskOperationCard } from "../../types/api";
import c from "./AskCards.module.css";
import { libraryHref } from "./libraryHref";

const PREVIEW_LINES = 12;

/** One long text, folded after PREVIEW_LINES until the operator unfolds it. */
function Folded({ text, testid }: { text: string; testid: string }) {
  const lines = text.split("\n");
  const [open, setOpen] = useState(false);
  const long = lines.length > PREVIEW_LINES;
  return (
    <>
      <div className={c.verbatim} data-testid={testid}>
        {long && !open ? lines.slice(0, PREVIEW_LINES).join("\n") : text}
      </div>
      {long ? (
        <button
          type="button"
          className={c.more}
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          data-testid="ask-library-more"
        >
          {open ? "Show less" : `Show all ${lines.length} lines`}
        </button>
      ) : null}
    </>
  );
}

function DiffBody({ text }: { text: string }) {
  return (
    <div className={c.diff} data-testid="ask-library-diff">
      {text.split("\n").map((row, i) => {
        const tone = row.startsWith("+") ? c.add : row.startsWith("-") ? c.del : row.startsWith("@@") ? c.hunk : "";
        // The sign stays in the text: added and removed are told apart by + and −, not colour.
        return (
          <div key={i} className={tone}>
            {row || " "}
          </div>
        );
      })}
    </div>
  );
}

const STATUS_LINE: Record<string, string> = {
  off: "Saved off: nothing runs until you enable it.",
  enabled: "Remains enabled: changes affect its next and later runs.",
  enable: "From then on it starts agents by itself, within exactly this scope.",
};

/** `onShown(true)` once the operator can see exactly what the card saves — and only then may the
 *  card offer Confirm (#1611 review): a digest proves the bytes didn't change, not that they were
 *  shown. A transient failure offers Retry; a 409 (the draft changed) never re-enables Confirm. */
export function LibraryContent({
  id,
  onShown,
}: {
  id: string;
  onShown: (shown: boolean) => void;
}) {
  const [content, setContent] = useState<AskLibraryContent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryable, setRetryable] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    // Confirm starts disabled (the card's own `shown` is false) and an error never enabled it,
    // so a retry has nothing to reset here.
    let live = true;
    api.askProposalContent(id).then(
      (body) => {
        if (!live) return;
        setContent(body);
        onShown(true);
      },
      (e: unknown) => {
        if (!live) return;
        setRetryable(!(e instanceof ApiError && e.status === 409));
        setError(
          e instanceof ApiError && e.status === 409
            ? "What this card saves changed after it was proposed. Don't confirm it; ask HQ again."
            : "What this saves could not be read just now, so it can't be confirmed yet.",
        );
      },
    );
    return () => {
      live = false;
    };
  }, [id, attempt, onShown]);
  if (error) {
    return (
      <div className={c.note} role="alert" data-testid="ask-library-error">
        <span>{error}</span>
        {retryable ? (
          <button
            type="button"
            className={c.ghost}
            onClick={() => {
              setError(null);
              setAttempt((n) => n + 1);
            }}
            data-testid="ask-library-retry"
          >
            Retry
          </button>
        ) : null}
      </div>
    );
  }
  if (!content) {
    return (
      <div className={c.note} data-testid="ask-library-loading">
        <span>Loading what it saves…</span>
      </div>
    );
  }
  if (content.kind === "files") {
    return (
      <div data-testid="ask-library-files">
        <div className={c.blockLabel}>Files, verbatim</div>
        {content.files.map((f) => (
          <div key={f.path} className={c.file}>
            <div className={c.fileHead}>
              <span>{f.path}</span>
              <span className={c.fileLines}>
                {f.text === null ? "not text" : `${f.text.split("\n").length} lines`}
              </span>
            </div>
            {f.text !== null ? <Folded text={f.text} testid="ask-library-file" /> : null}
          </div>
        ))}
      </div>
    );
  }
  if (content.kind === "diff") {
    return (
      <div data-testid="ask-library-changes">
        <div className={c.blockLabel}>Changes, verbatim</div>
        {content.diff.map((d) => (
          <div key={d.path} className={c.file}>
            <div className={c.fileHead}>
              <span>{d.path}</span>
              <span className={c.fileLines}>
                {d.status} · +{d.added} −{d.removed}
              </span>
            </div>
            {d.diff ? <DiffBody text={d.diff} /> : null}
          </div>
        ))}
      </div>
    );
  }
  return (
    <div data-testid="ask-library-automation">
      {content.name || content.changed.length ? (
        <dl className={c.kv} data-testid="ask-library-settings">
          {content.name ? (
            <>
              <dt>Name</dt>
              <dd>{content.name}</dd>
            </>
          ) : null}
          {content.changed.length ? (
            <>
              <dt>Changes</dt>
              <dd>{content.changed.join(", ")}</dd>
            </>
          ) : null}
        </dl>
      ) : null}
      <ul className={c.scope} aria-label="What it may do">
        {content.scope_lines.map((line, i) => (
          <li key={i}>{line}</li>
        ))}
      </ul>
      {content.widened.length ? (
        <div className={c.note} role="note" data-testid="ask-library-widened">
          <span>Wider than what you consented to: {content.widened.join("; ")}.</span>
        </div>
      ) : null}
      {content.instruction ? (
        <>
          <div className={c.blockLabel}>Instruction, verbatim</div>
          <Folded text={content.instruction} testid="ask-library-instruction" />
        </>
      ) : null}
      {STATUS_LINE[content.status] ? (
        <div className={c.statusLine} data-testid="ask-library-status">
          {STATUS_LINE[content.status]}
        </div>
      ) : null}
    </div>
  );
}

export function OpenInLibrary({ saved }: { saved: NonNullable<AskOperationCard["library"]> }) {
  return (
    <Link className={c.libraryLink} to={libraryHref(saved)} data-testid="ask-library-open">
      Open in Library
    </Link>
  );
}
