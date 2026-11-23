"""Readable text for a tool result's ``content``, wherever a transcript renders it.

A client reports a tool result as a bare string or a list of content blocks
(``{"type": "text", "text": …}``, an image, a tool reference, …). Rendering the raw JSON wrapper
shows block metadata instead of the text and spends a reader's output budget on it (#260, #1982).
One helper serves the terminal transcript, the native API fold and the native protocol's stored
``output``, so the three can never drift.
"""

from __future__ import annotations

import json


def result_text(content: object) -> str:
    """The text a tool result carries: str → itself; list/dict of text blocks → their joined text;
    a typed block without text → ``[type]``; a shapeless entry → ``[python type]``; anything else
    → compact JSON, so a result never renders empty (#260).

    A string is always returned as it is. The fold never re-parses a stored output: text that is
    itself JSON (even exactly the shape of a content block) cannot be told apart from an older
    journal's wrapper, so re-parsing would destroy real results (#1982 review). The producer
    unwraps list content when the result is first recorded instead.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                t = p.get("text") or p.get("content")
                if isinstance(t, str):
                    parts.append(t)
                elif p.get("type"):  # image / tool_reference / … → tag, never dump the blob
                    parts.append(f"[{p['type']}]")
                else:  # an untyped block still never folds to nothing
                    parts.append("[block]")
            else:  # a shapeless entry → its type, never nothing
                parts.append(f"[{type(p).__name__}]")
        if parts:
            return "\n".join(parts)
    if isinstance(content, dict):
        t = content.get("text") or content.get("content")
        if isinstance(t, str):
            return t
        if content.get("type"):
            return f"[{content['type']}]"
    return json.dumps(content, default=str) if content else ""
