"""Error-preserving enumeration for maintenance's measurements (#993).

Every engine's ``scan()`` is fail-soft twice over: the directory listing is wrapped in
``except OSError: return out``, and each record's own read returns ``None`` on failure, which is
indistinguishable from "this record is not listable". That is right for the sidebar — one
unreadable file must never blank the list — and wrong for a measurement that authorises DELETION,
where an unreadable store silently becomes "nothing to remove" (Hermes on PR #1000, reviews
4894/4898).

These are the checked counterparts. ``scan()`` keeps its own independent fail-soft path and must
never be routed through them: unifying the two is exactly what hid every healthy ``shell`` session
behind one malformed record (review 4898, finding 4).

A **leaf module on purpose** — it imports nothing from the app, so ``scanner.py`` can use it
without importing the ``engines`` package, which would be circular (``engines/__init__`` →
``registry`` → ``scanner``). ``engines.base`` re-exports both names, since providers already hold
``base``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path


def scandir_checked(root: Path, *, recurse: bool = False) -> Iterator[os.DirEntry]:
    """Entries under ``root``, RAISING when a directory cannot be read.

    ``Path.glob``/``rglob`` swallow a permission error on the directory itself and simply yield
    nothing, so an unreadable store and an empty one give the same answer. ``FileNotFoundError``
    stays absence — a store that was never created is legitimately empty.
    """
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return
    for entry in entries:
        yield entry
        if recurse and entry.is_dir(follow_symlinks=False):
            yield from scandir_checked(Path(entry.path), recurse=True)


def _readable(path: Path) -> None:
    """Raise if ``path``'s contents cannot be read. Directories are probed by listing them."""
    if path.is_dir():
        os.scandir(path).close()
    else:
        with path.open("rb"):
            pass


def checked_rows(
    paths: Iterable[Path],
    build: Callable[[Path], object | None],
    *,
    engine_id: str,
) -> tuple[list, list[str]]:
    """``(rows, problems)`` — every row that built, plus one line per record that could not be READ.

    **Partial by design.** A record that fails to read costs only itself; the rest are still
    returned, because dropping the whole provider for one bad file is the same defect in the other
    direction (review 4898, finding 4).

    Each path is probed for readability before its builder runs. The builder must also propagate
    failures from its own reads and stat calls: a successful probe cannot guarantee a later read
    succeeds. Only then does ``None`` mean read fine, not listable. A record that vanishes before
    the probe is gone rather than unreadable.
    """
    rows: list = []
    problems: list[str] = []
    for path in paths:
        try:
            _readable(path)
        except FileNotFoundError:
            continue  # vanished between the listing and the read — gone, not unreadable
        except OSError as e:
            problems.append(f"{engine_id}: {path.name} could not be read ({type(e).__name__})")
            continue
        try:
            row = build(path)
        except Exception as e:  # noqa: BLE001 — one unreadable record must not blank the rest
            # The builder's OWN authoritative read failed — kimi's `state.json`, antigravity's
            # SQLite lookup, gemini's chat file. Probing the path first is necessary and not
            # sufficient: readability at probe time does not make the later read succeed, and the
            # fail-soft builders turn that failure into `None`, which is indistinguishable from
            # "not listable" (review 4915/4919, finding 4). A checked builder propagates instead,
            # and this is where it becomes a named problem rather than a silent omission.
            problems.append(f"{engine_id}: {path.name} could not be read ({type(e).__name__})")
            continue
        if row is not None:
            rows.append(row)
    return rows, problems
