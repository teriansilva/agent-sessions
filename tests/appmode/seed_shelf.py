"""Seed a mission and its shelf for `web/e2e/appmode-mission-shelf.spec.ts` (#1740 P1a).

Runs with the app-mode stack's own environment (its HOME, missions store and file root), so the
real app reads what this writes. Items go in through `mission_shelf.deliver` — the one judge every
agent delivery passes (the tool and broker in front of it are pinned end to end by
`tests/test_mission_shelf.py`). Prints `{"mission_id", "ids": {name: item id}}` as one JSON line.

    uv run python tests/appmode/seed_shelf.py <repo> <session key>
"""

from __future__ import annotations

import json
import sys

from agent_sessions import mission_shelf, missions


def main(repo: str, session: str) -> int:
    m = missions.create_mission("Launch assets for v0.21", title="Launch assets", cwd=repo)
    missions.adopt(m["id"], session)

    def add(**item) -> str:
        return mission_shelf.deliver(m["id"], item, session_key=session, phys_key=session)["id"]

    ids = {
        "image": add(
            title="Poster frame", why="for the release page", path=f"{repo}/out/poster.png"
        ),
        "video": add(title="Hero video", why="the launch cut", path=f"{repo}/out/hero.webm"),
        "audio": add(title="Voice-over", why="take 3", path=f"{repo}/out/vo.ogg"),
        "doc": add(title="Changelog", why="grouped by area", path=f"{repo}/CHANGELOG.md"),
        "pdf": add(
            title="Release notes", why="for the press page", path=f"{repo}/out/release notes.pdf"
        ),
        "link": add(
            title="Staging preview",
            why="the page in place",
            url="https://staging.example.org/releases/v0-21",
        ),
        "text": add(
            title="Finding",
            why="worth a look",
            text="The badge is the old style.\n<b>Re-shoot</b> from staging.",
        ),
        "gone": add(title="Shelf page, dark", why="third screenshot", path=f"{repo}/out/gone.png"),
        "private": add(title="Private shot", why="moved later", path=f"{repo}/private/hidden.png"),
    }
    print(json.dumps({"mission_id": m["id"], "ids": ids}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
