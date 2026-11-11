"""Seed an artifact for `web/e2e/appmode-artifacts.spec.ts` (#1932 P1).

Runs with the app-mode stack's own environment (its HOME, stores and file root), so the real app
serves what this writes. The site goes in through `artifact_store.publish` — the one judge every
agent publish passes (the tool and broker in front of it are pinned end to end by
`tests/test_artifacts.py`). Prints `{"id": <artifact id>}` as one JSON line.

    uv run python tests/appmode/seed_artifacts.py <repo> <session key>
"""

from __future__ import annotations

import json
import sys

from agent_sessions import artifact_store, prefs


def main(repo: str, session: str) -> int:
    prefs.set_artifacts_enabled(True)
    origin = artifact_store.Origin(engine="claude", session_key=session, phys_key=session, cwd=repo)
    out = artifact_store.publish(
        origin, {"path": f"{repo}/site", "title": "Probe site", "why": "the sandbox, in a browser"}
    )
    print(json.dumps({"id": out["id"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
