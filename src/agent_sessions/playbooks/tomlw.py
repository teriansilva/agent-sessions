"""A deliberately small TOML writer for the playbook documents BattleLab itself rewrites (#1191).

The standard library reads TOML (`tomllib`) but cannot write it, and the playbook format is a
closed, simple value space: strings, integers, booleans, arrays and tables. So this writes exactly
that and REFUSES everything else (floats, dates, `None`, integers outside TOML's 64-bit range,
strings that are not valid Unicode, nesting past `MAX_DEPTH`) rather than approximating it.

**Every document is proven by re-reading it.** `dumps` parses its own output with `tomllib` and
compares the result to the input with a TYPE-STRICT equality (Python's `True == 1` would otherwise
let a boolean round-trip as an integer); any difference refuses. The writer can therefore be wrong
only by refusing, never by writing a document that means something else.

It does not keep comments or layout: a rewritten document is the data, re-emitted. Callers that
need the author's bytes (an edit that sends a file's text) never come through here.
"""

from __future__ import annotations

import re
import tomllib

MAX_DEPTH = 16
INT_MIN = -(2**63)
INT_MAX = 2**63 - 1
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


class TomlWriteError(ValueError):
    """The value cannot be written as a playbook TOML document."""


def _string(s: str) -> str:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise TomlWriteError("a string is not valid Unicode") from None
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _key(k: object) -> str:
    if not isinstance(k, str) or not k:
        raise TomlWriteError("a table key is a non-empty string")
    return k if _BARE_KEY_RE.fullmatch(k) else _string(k)


def _is_aot(v: object) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _inline(v: object, depth: int) -> str:
    if depth > MAX_DEPTH:
        raise TomlWriteError(f"the document is nested deeper than {MAX_DEPTH}")
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        if not INT_MIN <= v <= INT_MAX:
            raise TomlWriteError("an integer is outside TOML's 64-bit range")
        return str(v)
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, list):
        return "[" + ", ".join(_inline(x, depth + 1) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{ " + ", ".join(f"{_key(k)} = {_inline(x, depth + 1)}" for k, x in v.items()) + " }"
    raise TomlWriteError(
        f"a {type(v).__name__} cannot be written (strings, integers, booleans, arrays and "
        "tables only)"
    )


def _table(d: dict, path: list[str], out: list[str], depth: int) -> None:
    if depth > MAX_DEPTH:
        raise TomlWriteError(f"the document is nested deeper than {MAX_DEPTH}")
    for k, v in d.items():
        if not isinstance(v, dict) and not _is_aot(v):
            out.append(f"{_key(k)} = {_inline(v, depth + 1)}")
    for k, v in d.items():
        if isinstance(v, dict):
            sub = [*path, _key(k)]
            out.append("")
            out.append(f"[{'.'.join(sub)}]")
            _table(v, sub, out, depth + 1)
    for k, v in d.items():
        if _is_aot(v):
            sub = [*path, _key(k)]
            for item in v:
                out.append("")
                out.append(f"[[{'.'.join(sub)}]]")
                _table(item, sub, out, depth + 1)


def strict_equal(a: object, b: object) -> bool:
    """Equality that also compares scalar TYPES (so `True` never equals `1`)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def dumps(doc: object) -> str:
    """`doc` as TOML text, proven to parse back to exactly `doc`, or `TomlWriteError`."""
    if not isinstance(doc, dict):
        raise TomlWriteError("a TOML document is a table")
    out: list[str] = []
    try:
        _table(doc, [], out, 0)
    except RecursionError:
        raise TomlWriteError(f"the document is nested deeper than {MAX_DEPTH}") from None
    text = "\n".join(out).lstrip("\n") + "\n"
    try:
        back = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        raise TomlWriteError("the document cannot be written as TOML") from None
    if not strict_equal(back, doc):
        raise TomlWriteError("the document cannot be written as TOML without changing it")
    return text
