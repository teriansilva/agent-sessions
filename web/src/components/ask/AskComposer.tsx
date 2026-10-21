/** Ask's field (#1171) — one box, used by the dashboard (where asking OPENS a conversation) and by
 *  the conversation page (where it continues one).
 *
 *  **Enter sends; Shift+Enter is a new line.** It is a chat, and the operator asked for the chat
 *  default. Ctrl/⌘+Enter still sends too, so a hand trained on the old shortcut is not surprised.
 *  An Enter that finishes an IME composition (`isComposing`, or the legacy keyCode 229) is the
 *  composition's own, never a send — otherwise confirming a Japanese word would submit half a
 *  question.
 *
 *  The box, its foot and its Send are the console's primitives (`mission.module.css`) and the
 *  session pane's Send (#967), as before — one box, drawn one way, wherever it appears.
 */
import { Send } from "lucide-react";
import { useState, type Ref } from "react";

import compose from "../terminal/Compose.module.css";
import styles from "../pulse/mission.module.css";
import a from "./AskConsole.module.css";

export function AskComposer({
  configured,
  busy = false,
  onAsk,
  inputRef,
}: {
  /** False when no AI endpoint is configured: the field is disabled and says why. */
  configured: boolean;
  /** A question is in flight: typing stays possible, sending does not. */
  busy?: boolean;
  onAsk: (question: string) => void;
  inputRef?: Ref<HTMLTextAreaElement>;
}) {
  const [text, setText] = useState("");
  const canSend = configured && !busy && text.trim() !== "";

  const send = () => {
    const q = text.trim();
    if (!q || busy || !configured) return;
    setText("");
    onAsk(q);
  };

  return (
    <form
      className={styles.composerBox}
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
      data-testid="ask-form"
    >
      <textarea
        ref={inputRef}
        className={`${styles.composerInput} ${styles.boxInput}`}
        rows={1}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key !== "Enter" || e.shiftKey) return;
          if (e.nativeEvent.isComposing || e.keyCode === 229) return;
          e.preventDefault();
          e.currentTarget.form?.requestSubmit();
        }}
        aria-keyshortcuts="Enter"
        disabled={!configured}
        placeholder={
          configured
            ? "Ask about your sessions and missions…"
            : "Needs an AI endpoint"
        }
        aria-label="Ask about your past work"
        data-testid="composer-input"
      />
      <div className={styles.composerFoot} data-testid="composer-foot">
        <div className={styles.footLead}>
          {/* The shortcut, for the eye. The textarea's `aria-keyshortcuts` is its accessible
              form. */}
          <span className={styles.footHint} aria-hidden="true">
            Enter to send
            <span className={a.hintMore}> · Shift + Enter new line</span>
          </span>
        </div>
        <div className={styles.footTrail}>
          <span className={styles.footSpacer} aria-hidden="true" />
          <button
            type="submit"
            className={`${compose.send} shine`}
            disabled={!canSend}
            data-testid="composer-send"
          >
            <Send size={15} aria-hidden="true" />
            Send
          </button>
        </div>
      </div>
    </form>
  );
}
