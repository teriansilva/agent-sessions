/** A RUNNING turn's reply (#1727, docs/design.md §8 rule 2): the whole text is in the DOM the
 *  moment it arrives, formatted as Markdown; only the words added since the last render fade in
 *  (Full), and while the turn streams a caret rides the end of its newest message. Reduced and Off
 *  show the same text with a steady caret. A finished reply is plain `<Markdown>`.
 *
 *  `appear`: whether the text it mounts with is new (a message that began after the pane had
 *  painted). The reply already on screen at the first paint is history and never fades. */
import { memo } from "react";

import { RevealText } from "../ask/RevealText";
import { markdownTree } from "./markdownTree";

export const LiveReply = memo(function LiveReply({
  text,
  caret,
  appear,
}: {
  text: string;
  caret: boolean;
  appear: boolean;
}) {
  return (
    <RevealText
      text={text}
      streaming={caret}
      appear={appear}
      markup={markdownTree}
      testId="structured-live-reply"
    />
  );
});
