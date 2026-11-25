"""Drift guard: the committed cross-impl vectors must match the Python impl.

If this fails, regenerate with ``python3 scripts/gen_homefree_vectors.py`` (the
browser mirror is tested against the same fixture in web/).
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

import gen_homefree_vectors as gen  # noqa: E402


def test_committed_vectors_match_python_impl():
    fixture = _ROOT / "web" / "src" / "homefree" / "handshake.vectors.json"
    committed = json.loads(fixture.read_text())
    rebuilt = json.loads(json.dumps(gen.build_vectors()))
    assert (
        committed == rebuilt
    ), "handshake vectors drifted — regenerate: python3 scripts/gen_homefree_vectors.py"
