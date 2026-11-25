"""`e2e_scope.py` (#2072): which Playwright specs a web PR needs.

Scoping too narrowly lets a UI break merge unreviewed (main's full run catches it, one merge late);
scoping too widely is the 40-minute wait this exists to end. The rules pinned here: every doubt is
the full suite, a change reaches the specs that address what it renders, and a file no spec can
observe needs none.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".forgejo" / "scripts"
REPO = SCRIPTS.parents[1]


def _load(name: str):
    import sys

    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


scope = _load("e2e_scope")


@pytest.fixture(autouse=True)
def _not_in_a_ci_event(monkeypatch):
    """The suite itself runs in CI, where the runner sets GITHUB_EVENT_NAME (`push` on main)."""
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root / "web"


# A small app: a shell, two pages, a shared button, a type-only module, a helper and four specs.
APP = {
    "web/index.html": '<script type="module" src="/src/main.tsx"></script>',
    "web/share.html": '<script type="module" src="/src/share/main.tsx"></script>',
    "web/src/main.tsx": 'import { App } from "./app/App";\n',
    "web/src/lib/routes.ts": 'export const BOARD_PATH = "/board";\n',
    "web/src/app/App.tsx": (
        'import { Board } from "../routes/Board";\n'
        'import { Term } from "../routes/Term";\n'
        'export function App() { return <div data-testid="app-shell">'
        "<Route path={BOARD_PATH} element={<Board />} />"
        '<Route path="/s/:id" element={<Term />} /></div>; }\n'
    ),
    "web/src/routes/Board.tsx": (
        'import { Card } from "../components/Card";\n'
        'export function Board() { return <section data-testid="board-page"><Card /></section>; }\n'
    ),
    "web/src/components/Card.tsx": (
        'import styles from "./Card.module.css";\n'
        'import type { CardData } from "../types/card";\n'
        'export function Card() { return <div data-testid="card-row" className={styles.c} />; }\n'
    ),
    "web/src/components/Card.module.css": ".c { color: red; }\n",
    "web/src/components/Card.test.tsx": 'import { Card } from "./Card";\n',
    "web/src/types/card.ts": "export interface CardData { id: string }\nexport type Id = string;\n",
    "web/src/routes/Term.tsx": (
        'import { Rows } from "../components/Rows";\n'
        "export function Term() { return <Rows />; }\n"
    ),
    "web/src/components/Rows.tsx": 'export function Rows() { return <div className="xterm" />; }\n',
    "web/src/share/main.tsx": 'import { Viewer } from "./Viewer";\n',
    "web/src/share/Viewer.tsx": "export function Viewer() { return <div />; }\n",
    "web/src/orphan.ts": "export const unused = 1;\n",
    "web/e2e/helper.ts": 'export const go = () => "/board";\n',
    "web/e2e/data.json": "{}\n",
    "web/e2e/board.spec.ts": 'import { go } from "./helper";\npage.getByTestId("board-page");\n',
    "web/e2e/card.spec.ts": "page.locator('[data-testid=card-row]');\n",
    "web/e2e/term.spec.ts": 'await page.goto("/s/abc"); page.locator(".xterm");\n',
    "web/e2e/share.spec.ts": 'r.fetch("/share.html"); load("data.json");\n',
    "web/e2e/rows-word.spec.ts": "// the word rows, and card-row-extra, are not testid uses\n",
}


@pytest.fixture
def web(tmp_path):
    return _tree(tmp_path, APP)


def _decide(web, *paths, labels=None):
    return scope.decide(list(paths), web, labels)


@pytest.mark.parametrize(
    "path",
    [
        "web/package.json",
        "web/package-lock.json",
        "web/vite.config.ts",
        "web/playwright.config.ts",
        "web/index.html",
        "web/src/main.tsx",
        "web/src/index.css",
        "web/src/tokens.css",
        "web/src/theme/applyTheme.ts",
        "web/src/app/App.tsx",
        "web/public/favicon.svg",
        "web/e2e/fixtures/api-look-baseline.json",
        ".forgejo/workflows/web-ci.yml",
        ".forgejo/scripts/e2e_scope.py",
        "scripts/e2e_slot_run",
    ],
)
def test_the_harness_build_and_shell_run_everything(web, path):
    mode, specs, reason = _decide(web, path)
    assert (mode, specs) == ("full", []), reason


def test_every_doubt_is_the_full_suite(web):
    assert scope.decide(None, web)[0] == "full"  # the list could not be read
    assert scope.decide([], web)[0] == "full"  # the PR lists nothing
    assert _decide(web, "web/src/a\nb.tsx")[0] == "full"  # control characters
    assert _decide(web, *[f"web/src/f{i}.tsx" for i in range(61)])[0] == "full"
    assert _decide(web, "web/src/orphan.ts")[0] == "full"  # reaches no spec
    assert _decide(web, "web/src/routes/Board.tsx", labels=["ci:full"])[0] == "full"


def test_main_fails_closed_when_the_api_is_down(monkeypatch, tmp_path, capsys):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("API", "https://127.0.0.1:9/api")  # nothing listens
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("TOKEN", "t")
    assert scope.main([]) == 0
    text = out.read_text()
    assert "mode=full\n" in text and "shards=1 2 3 4\n" in text


def test_a_component_reaches_the_specs_addressing_it_and_its_pages(web):
    mode, specs, _ = _decide(web, "web/src/components/Card.tsx")
    assert mode == "subset"
    # card.spec names its testid; board.spec names the page that renders it. The shell's own
    # testid is on every page, so it selects nothing; a bare word is not a testid use.
    assert specs == ["e2e/board.spec.ts", "e2e/card.spec.ts"]


def test_a_stylesheet_is_judged_by_the_component_importing_it(web):
    assert _decide(web, "web/src/components/Card.module.css")[1] == [
        "e2e/board.spec.ts",
        "e2e/card.spec.ts",
    ]


def test_a_file_with_no_testid_falls_back_to_the_route_it_renders_under(web):
    assert _decide(web, "web/src/components/Rows.tsx")[1] == ["e2e/term.spec.ts"]


def test_an_entry_module_reaches_the_specs_of_its_page(web):
    assert _decide(web, "web/src/share/Viewer.tsx")[1] == ["e2e/share.spec.ts"]


def test_specs_helpers_and_data_files(web):
    assert _decide(web, "web/e2e/term.spec.ts")[1] == ["e2e/term.spec.ts"]
    assert _decide(web, "web/e2e/helper.ts")[1] == ["e2e/board.spec.ts"]
    assert _decide(web, "web/e2e/data.json")[1] == ["e2e/share.spec.ts"]


def test_what_no_spec_can_observe_needs_none(web):
    for path in (
        "web/src/components/Card.test.tsx",
        "web/README.md",
        "web/visual/nightjar/manifest.mjs",
        "src/agent_sessions/main.py",  # web-ci's previews never reach the backend
    ):
        assert _decide(web, path)[0] == "none", path


def test_a_deleted_file_is_judged_by_the_files_that_used_it(web):
    assert _decide(web, "web/src/components/Gone.tsx")[0] == "none"
    assert _decide(web, "web/src/components/Gone.tsx", "web/src/components/Card.tsx")[0] == "subset"


def test_shards_follow_the_spec_count():
    assert scope.shards_for(1) == [1]
    assert scope.shards_for(25) == [1]
    assert scope.shards_for(26) == [1, 2]
    assert scope.shards_for(80) == [1, 2, 3, 4]
    assert scope.shards_for(300) == [1, 2, 3, 4]


def test_the_output_lists_safe_spec_paths_and_active_shards(web, monkeypatch, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(
        scope,
        "decide",
        lambda paths, w, labels=None: (
            "subset",
            ["e2e/a.spec.ts", "e2e/$(x).spec.ts", "e2e/b c.spec.ts"],
            "r\nmode=none",
        ),
    )
    scope.main(["--files", "web/x.tsx"])
    lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert lines["mode"] == "subset"
    assert lines["specs"] == "e2e/a.spec.ts"  # an unsafe path never reaches the shell
    assert lines["shards"] == "1" and lines["count"] == "1"
    assert lines["reason"] == "r mode=none"  # one line: it cannot inject a second key


def test_the_ci_full_label_is_read(monkeypatch):
    class R:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps(self.body).encode()

    seen = {}

    def fake(req, timeout):
        seen["auth"] = req.headers.get("Authorization")
        return R([{"name": "type:chore"}, {"name": "ci:full"}])

    monkeypatch.setattr(scope.urllib.request, "urlopen", fake)
    assert scope.pr_labels("https://x/api", "7", "tok") == ["type:chore", "ci:full"]
    assert seen["auth"] == "token tok"  # a header, never a command line


# --- the real tree --------------------------------------------------------------------------


def test_the_real_tree_scopes_a_public_page_change_narrowly():
    mode, specs, reason = scope.decide(
        ["web/src/share/ShareViewer.tsx", "web/src/reply/ReplyViewer.tsx"], REPO / "web"
    )
    assert mode == "subset", reason
    assert "e2e/public-share.spec.ts" in specs and "e2e/reply-share.spec.ts" in specs
    assert len(specs) < 60, specs


def test_the_real_route_table_is_read():
    routes = scope._route_prefixes((REPO / "web").resolve())
    by_name = {p.stem: v for p, v in routes.items()}
    assert "/s" in by_name["SessionView"]
    assert "/settings" in by_name["Settings"]
    assert len(routes) >= 15  # if App.tsx's route syntax drifts, this says so


def test_a_push_to_main_is_the_full_backstop(monkeypatch, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    assert scope.main([]) == 0
    assert "mode=full\n" in out.read_text() and "shards=1 2 3 4\n" in out.read_text()


def test_a_spec_name_that_cannot_be_passed_safely_runs_everything(web):
    (web / "e2e" / "new test.spec.ts").write_text('page.getByTestId("card-row");\n')
    mode, specs, reason = _decide(web, "web/e2e/new test.spec.ts")
    assert (mode, specs) == ("full", []), reason  # never silently dropped
    # Mixed with safe specs, too: a selection that drops one spec is not a selection.
    mode, _, _ = _decide(web, "web/src/components/Card.tsx")
    assert mode == "full"


@pytest.mark.parametrize(
    "path",
    [
        ".forgejo/scripts/recheck.py",
        ".forgejo/scripts/sibling_watch.py",
        ".forgejo/pytest_plugins/recheck_report.py",
    ],
)
def test_every_script_the_workflow_runs_is_a_full_trigger(web, path):
    assert _decide(web, path)[0] == "full"


def test_a_types_module_is_judged_by_the_files_importing_it(web):
    # No "type-only" shortcut (Hermes 8267/8276): a regex can't prove a TypeScript module runs no
    # code, so a types module selects the specs of the components that import it, like any file.
    assert _decide(web, "web/src/types/card.ts")[1] == ["e2e/board.spec.ts", "e2e/card.spec.ts"]
    one_line = web / "src" / "types" / "flags.ts"
    one_line.write_text('export interface F { on: boolean }; document.body.dataset.x = "0";\n')
    card = web / "src" / "components" / "Card.tsx"
    card.write_text('import "../types/flags";\n' + card.read_text())
    assert "e2e/card.spec.ts" in _decide(web, "web/src/types/flags.ts")[1]


def test_a_side_effecting_module_with_type_exports_still_selects_its_specs(web):
    (web / "src" / "components" / "flags.ts").write_text(
        'export interface Flags { on: boolean }\ndocument.body.dataset.enabled = "false";\n'
    )
    card = web / "src" / "components" / "Card.tsx"
    card.write_text('import "./flags";\n' + card.read_text())
    mode, specs, _ = _decide(web, "web/src/components/flags.ts")
    assert mode == "subset" and "e2e/card.spec.ts" in specs


def test_a_spec_that_exports_a_type_is_still_run(web):
    (web / "e2e" / "typed.spec.ts").write_text(
        "export type Row = { id: string };\ntest('x', () => {});\n"
    )
    assert _decide(web, "web/e2e/typed.spec.ts")[1] == ["e2e/typed.spec.ts"]
