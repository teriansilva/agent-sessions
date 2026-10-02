"""Validate an extracted, pinned npm dependency closure without invoking npm (#1259).

The supported dependency language is ordinary registry semver (exact, partial, wildcard, caret,
tilde, comparisons, hyphen ranges and unions) plus npm aliases. URLs, workspace/file links and
tags are refused. Install scripts are never executed; a package that needs one must publish an
already built artifact or fail its real verification before enable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import budget, feed

_NAME = r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*"
_NAME_RE = re.compile(_NAME)
_DEST_RE = re.compile(rf"node_modules/{_NAME}(?:/node_modules/{_NAME})*")
_VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)


def destination(value: str) -> str:
    if not _DEST_RE.fullmatch(value):
        raise feed.FeedError("invalid npm package destination")
    return value


def _partial(text: str) -> tuple[tuple[int, int, int], int]:
    text = text.removeprefix("v")
    parts = text.split(".")
    if len(parts) > 3:
        raise feed.FeedError("unsupported npm dependency range")
    values = []
    wildcard = False
    for part in parts:
        if part.lower() in ("x", "*"):
            wildcard = True
        elif wildcard or not re.fullmatch(r"0|[1-9][0-9]{0,8}", part):
            raise feed.FeedError("unsupported npm dependency range")
        else:
            values.append(int(part))
    count = len(values)
    return tuple((values + [0, 0, 0])[:3]), count


def _upper(base: tuple[int, int, int], count: int) -> tuple[int, int, int]:
    return (base[0] + 1, 0, 0) if count <= 1 else (base[0], base[1] + 1, 0)


def _version(text: str) -> tuple:
    match = _VERSION.fullmatch(text.removeprefix("v"))
    if not match:
        raise feed.FeedError("invalid npm package version")
    identifiers = ()
    if match[4]:
        parts = match[4][1:].split(".")
        if any(not p or (p.isdigit() and len(p) > 1 and p[0] == "0") for p in parts):
            raise feed.FeedError("invalid npm prerelease version")
        identifiers = tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)
    if "+" in text and any(not p for p in text.split("+", 1)[1].split(".")):
        raise feed.FeedError("invalid npm build metadata")
    return (*(int(match[i]) for i in range(1, 4)), not bool(match[4]), identifiers)


def _stable(base: tuple[int, int, int]) -> tuple:
    return (*base, True, ())


def _ceiling(base: tuple[int, int, int]) -> tuple:
    return (*base, False, ((0, 0),))


def _operand(literal: str) -> tuple[tuple, int]:
    if "-" in literal or "+" in literal:
        return _version(literal), 3
    base, count = _partial(literal)
    return _stable(base), count


def _compile(spec: str) -> list[tuple[list[tuple[str, tuple]], set[tuple]]]:
    if not isinstance(spec, str) or not 0 < len(spec) <= 512:
        raise feed.FeedError("unsupported npm dependency range")
    alternatives = spec.split("||")
    if len(alternatives) > 8:
        raise feed.FeedError("too many npm range alternatives")
    compiled = []
    # Parse EVERY arm before any evaluation. A matching first arm cannot hide an unsupported
    # tag/URL/file dependency in a later arm, including optional dependencies absent on this OS.
    for alternative in alternatives:
        tests, anchors = [], set()
        hyphen = alternative.strip().split(" - ")
        if len(hyphen) == 2:
            low, _ = _operand(hyphen[0])
            high, count = _operand(hyphen[1])
            tests.append((">=", low))
            tests.append(("<=", high) if count == 3 else ("<", _ceiling(_upper(high[:3], count))))
            anchors.update(v[:3] for v in (low, high) if not v[3])
        else:
            tokens = re.findall(r"(?:>=|<=|>|<|\^|~|=)?\s*[^\s]+", alternative)
            if not tokens or len(tokens) > 8:
                raise feed.FeedError("unsupported npm dependency range")
            for token in tokens:
                token = re.sub(r"\s", "", token)
                match = re.fullmatch(r"(>=|<=|>|<|\^|~|=)?(.+)", token)
                op, literal = match[1] or "", match[2]
                value, count = _operand(literal)
                base = value[:3]
                if not value[3]:
                    anchors.add(base)
                if count == 0:
                    if op not in ("", "=", "~", "^", ">=", "<="):
                        tests.append(("<", _stable((0, 0, 0))))
                elif op in ("", "="):
                    tests.extend(
                        [("=", value)]
                        if count == 3
                        else [(">=", value), ("<", _ceiling(_upper(base, count)))]
                    )
                elif op in ("^", "~"):
                    upper = _upper(base, count) if op == "~" else (base[0] + 1, 0, 0)
                    if op == "^" and base[0] == 0 and count > 1:
                        upper = (0, base[1] + 1, 0)
                        if base[1] == 0 and count == 3:
                            upper = (0, 0, base[2] + 1)
                    tests.extend([(">=", value), ("<", _ceiling(upper))])
                elif op in (">=", "<") or count == 3:
                    tests.append((op, _ceiling(base) if op == "<" and count < 3 else value))
                else:
                    tests.append(
                        (">=", _stable(_upper(base, count)))
                        if op == ">"
                        else ("<", _ceiling(_upper(base, count)))
                    )
        compiled.append((tests, anchors))
    return compiled


def satisfies(version: str, spec: str) -> bool:
    value = _version(version)
    alternatives = _compile(spec)
    for tests, anchors in alternatives:
        # npm admits prereleases only when this comparator set names their same base version.
        if not value[3] and value[:3] not in anchors:
            continue
        if all(
            {
                "=": value == bound,
                ">=": value >= bound,
                "<=": value <= bound,
                ">": value > bound,
                "<": value < bound,
            }[op]
            for op, bound in tests
        ):
            return True
    return False


def _read(path: Path) -> dict:
    with path.open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise feed.FeedError("npm package metadata is too large")
    try:
        doc = json.loads(raw, object_pairs_hook=feed._pairs, parse_constant=feed._no_float)
    except (ValueError, UnicodeError, RecursionError):
        raise feed.FeedError("invalid npm package metadata") from None
    if (
        not isinstance(doc, dict)
        or not isinstance(doc.get("name"), str)
        or not _NAME_RE.fullmatch(doc["name"])
        or not isinstance(doc.get("version"), str)
        or not _VERSION.fullmatch(doc["version"])
    ):
        raise feed.FeedError("npm package needs a registry name and exact version")
    _version(doc["version"])
    return doc


def validate(root: Path, entry: feed.Entry) -> None:
    if entry.manifest.install.kind != "npm-prefix":
        return
    packages = {}
    for path in root.rglob("package.json"):
        budget.check()
        relative = str(path.parent.relative_to(root))
        if _DEST_RE.fullmatch(relative):
            packages[relative] = _read(path)
    for artifact in entry.artifacts:
        budget.check()
        dest = destination(artifact.destination)
        doc = packages.get(dest)
        if doc is None:
            raise feed.FeedError("a declared npm artifact has no package metadata")
        name, version = doc["name"], doc["version"]
        expected = f"/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"
        if urlsplit(artifact.url).path != expected:
            raise feed.FeedError("npm metadata disagrees with the pinned distribution URL")
    for location, doc in packages.items():
        budget.check()
        required = doc.get("dependencies", {})
        optional = doc.get("optionalDependencies", {})
        peers = doc.get("peerDependencies", {})
        peer_meta = doc.get("peerDependenciesMeta", {})
        if not all(isinstance(d, dict) for d in (required, optional, peers, peer_meta)):
            raise feed.FeedError("invalid npm dependency metadata")
        dependencies = [
            (name, spec, False) for name, spec in required.items() if name not in optional
        ]
        dependencies += [(name, spec, True) for name, spec in optional.items()]
        dependencies += [
            (
                name,
                spec,
                isinstance(peer_meta.get(name), dict) and peer_meta[name].get("optional") is True,
            )
            for name, spec in peers.items()
        ]
        for name, spec, optional_dependency in dependencies:
            budget.check()
            if not _NAME_RE.fullmatch(name) or not isinstance(spec, str):
                raise feed.FeedError("invalid npm dependency")
            expected_name = name
            if spec.startswith("npm:"):
                expected_name, sep, spec = spec[4:].rpartition("@")
                if not sep or not _NAME_RE.fullmatch(expected_name):
                    raise feed.FeedError("invalid npm alias")
            _compile(spec)
            found = None
            parent = Path(location)
            while True:
                if parent.name != "node_modules":
                    found = packages.get(str(parent / "node_modules" / name))
                    if found:
                        break
                if parent == Path("."):
                    break
                parent = parent.parent
            if found is None:
                if optional_dependency:
                    continue
                raise feed.FeedError(f"the offline closure is missing dependency {name}")
            if found["name"] != expected_name or not satisfies(found["version"], spec):
                raise feed.FeedError(f"the pinned dependency does not satisfy {name}")
