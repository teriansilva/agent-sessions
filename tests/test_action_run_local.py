"""Run locally (#2248 P3): Run a command and Run a script.

The run is real: short-lived processes this test starts (a Python program that echoes its argv
and environment as JSON, scripts that spawn a child and a grandchild). Nothing here kills by name;
the code under test kills only the process group it created (validated) and its own scope.

Containment: most tests stand in for `scopedspawn.wrap` with a fake unit (the runner host may have
no user systemd), so the process tree is stopped by its validated process group. One test uses a
REAL transient scope when this host has one, and proves a descendant that left the process group is
killed through the scope.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import inspect
import json
import os
import time
from pathlib import Path

import pytest

from agent_sessions import automation_effect_lock as effect_lock
from agent_sessions import automation_runner, scopedspawn, webhooks
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions.actions import plugins, registry, run_command, run_local, run_script
from agent_sessions.routes import automations as routes
from test_webhook_events import PR, _deliver, _runs

NAME = "Nightly build"
FAKE_UNIT = "as-action-test0000-deadbeef.scope"

ECHO = """\
import json, os, sys
print("hello from the program")
print(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}))
"""

TREE = """\
#!/usr/bin/python3
import os, subprocess, sys, time
pids = sys.argv[1]
child = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; "
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
    "start_new_session=(sys.argv[1] == 'escape')); print(g.pid, flush=True); time.sleep(60)",
    sys.argv[2]], stdout=subprocess.PIPE, text=True)
grand = child.stdout.readline().strip()
with open(pids + ".tmp", "w") as f:
    f.write(f"{os.getpid()} {child.pid} {grand}")
os.rename(pids + ".tmp", pids)
time.sleep(60)
"""


@pytest.fixture(autouse=True)
def _loop_on(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    monkeypatch.setattr(effect_lock, "WRITER_WAIT_S", 0.3)


@pytest.fixture
def spawned(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(automation_runner, "spawn", lambda run, registry=None: out.append(run))
    return out


@pytest.fixture
def contained(monkeypatch):
    """Containment present, with a fake scope: the tree is stopped through its process group."""
    calls: dict[str, list] = {"wrap": [], "scope_kill": []}

    def wrap(argv, *, engine, session_id):
        calls["wrap"].append((list(argv), engine, session_id))
        return list(argv), FAKE_UNIT

    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)
    monkeypatch.setattr(scopedspawn, "wrap", wrap)
    monkeypatch.setattr(run_local, "_scope_kill", lambda unit: calls["scope_kill"].append(unit))
    return calls


@pytest.fixture
def shop(tmp_path, monkeypatch, contained):
    from agent_sessions import projects

    monkeypatch.setenv("HOME", str(tmp_path))
    # A secret-looking variable in the APP's environment: it must never reach the program.
    monkeypatch.setenv("AGENT_SESSIONS_FAKE_TOKEN", "app-only-value")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 36)
    root = tmp_path / "shop"
    (root / "scripts").mkdir(parents=True)
    (root / "echo.py").write_text(ECHO)
    _script(root / "scripts" / "build.sh", '#!/bin/sh\necho building "$1"\n')
    _script(root / "scripts" / "tree.py", TREE)
    other = tmp_path / "elsewhere"
    other.mkdir()
    _script(other / "evil.sh", "#!/bin/sh\necho evil\n")
    proj = projects.create("shop", folders=[str(root)], default_folder=str(root))
    return {"id": proj.id, "root": root, "other": other, "tmp": tmp_path}


def _script(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def _plugin(pid: str, inputs: dict, mapped: dict | None = None) -> dict:
    a = {"kind": "plugin", "plugin": pid, "version": 1, "inputs": inputs}
    return {**a, "from_delivery": mapped} if mapped else a


def _config(action: dict, trigger: dict | None = None) -> dict:
    return {
        "name": NAME,
        "trigger": trigger or {"kind": "manual"},
        "action": action,
        "policy": {"concurrency": "allow", "max_concurrent": 2},
    }


def _command(pid: str, argv: list, mapped=None, **extra) -> dict:
    return _plugin("run_command", {"project_id": pid, "argv": argv, **extra}, mapped)


def _echo(shop, *more, mapped=None, **extra) -> dict:
    # Relative to the run's cwd, the project's folder (a tmp path can read as random to the
    # credential check every fixed argument goes through).
    argv = [{"value": "python3"}, {"value": "echo.py"}, *more]
    return _command(shop["id"], argv, mapped, **extra)


def _scripted(pid: str, script: str = "scripts/build.sh", args=None, mapped=None, **extra):
    inputs = {"project_id": pid, "script": script, **extra}
    if args is not None:
        inputs["args"] = args
    return _plugin("run_script", inputs, mapped)


def _made(cfg: dict) -> str:
    aid = routes._create(copy.deepcopy(cfg))["id"]
    pub = routes.public(store.get(aid))
    routes._enable(
        aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
    )
    return aid


def _manual_run(aid: str) -> dict:
    res = store.begin_run(aid, trigger="manual", slot=f"manual:{time.time_ns()}", fire_at=None)
    assert res["claimed"], res
    return res["run"]


def _execute(run_: dict) -> dict:
    return asyncio.run(automation_runner.execute(run_, registry=None))


def _recorded(aid: str) -> dict:
    return store.get_run(_runs(aid)[0]["id"])["inputs"]


def _echoed(aid: str) -> dict:
    out = _recorded(aid)["output"]["stdout"]
    return json.loads(out.strip().split("\n")[-1])


def _hooked(shop, action: dict) -> tuple[str, str]:
    pub, _ = webhooks.create("ci")
    return _made(_config(action, trigger={"kind": "webhook", "webhook_id": pub["id"]})), pub["id"]


def _alive(pid: int) -> bool:
    try:
        st = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return st.rsplit(")", 1)[1].split()[0] != "Z"


def _gone(pids: list[int], within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


def _wait_file(p: Path, within: float = 10.0) -> str:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if p.exists():
            return p.read_text()
        time.sleep(0.02)
    raise AssertionError(f"{p} never appeared")


# ---- the manifests and the contract -------------------------------------------------------------


def test_both_are_run_local_plugins_in_the_gallery(contained):
    gallery = registry.gallery()
    rows = {a["id"]: a for a in gallery["actions"]}
    for pid in ("run_command", "run_script"):
        assert pid in plugins.PLUGINS
        assert rows[pid]["effect"] == "run-local" and rows[pid]["category"] == "run_locally"
        req = {r["id"]: r for r in rows[pid]["requires"]}
        assert req["containment"]["ok"] is True
    assert "run_locally" in {c["id"] for c in gallery["categories"]}


def test_the_gallery_says_when_containment_is_missing(monkeypatch):
    monkeypatch.setattr(scopedspawn, "enabled", lambda: False)
    row = next(a for a in registry.gallery()["actions"] if a["id"] == "run_command")
    req = {r["id"]: r for r in row["requires"]}
    assert req["containment"]["ok"] is False and not row["available"]
    assert "switched off" in req["containment"]["detail"]


@pytest.mark.parametrize(
    "inputs, why",
    [
        ({"argv": []}, "the program, is always fixed"),
        ({"argv": [{"from": "x"}]}, "the program, is always fixed"),
        ({"argv": [{"value": "make", "option": True}]}, "the program, not an option"),
        ({"argv": [{"value": "./build.sh"}]}, "use Run a script"),
        ({"argv": [{"value": "make"}, {"from": "x", "option": True}]}, "never an option"),
        (
            {"argv": [{"value": "python3"}, {"value": "-c", "option": True}, {"from": "x"}]},
            "value of the option '-c': it is always fixed",
        ),
        ({"argv": [{"value": "make"}, {"value": "a\x00b"}]}, "printable"),
        ({"argv": [{"value": "make"}], "env": [{"name": "LD_PRELOAD", "from": "x"}]}, "read as"),
        ({"argv": [{"value": "make"}], "env": [{"name": "BASH_ENV", "from": "x"}]}, "read as"),
        ({"argv": [{"value": "make"}], "env": [{"name": "PATH", "value": "/tmp"}]}, "set by the"),
        ({"argv": [{"value": "make"}], "env": [{"name": "a-b", "value": "1"}]}, "A-Z"),
        ({"argv": [{"value": "make"}], "timeout_minutes": 31}, "1 to 30"),
        ({"argv": [{"value": "make"}], "timeout_minutes": 0}, "1 to 30"),
        ({"argv": [{"value": "make"}], "outputs": {"exit_code": "number"}}, "always an output"),
        ({"argv": [{"value": "make"}], "outputs": {"x": "link"}}, "type must be"),
        ({"argv": [{"value": "make"}], "shell": True}, "does not take shell"),
    ],
)
def test_run_command_validates_strictly(inputs, why):
    mapped = {"x": {"path": "a.b", "type": "text"}}
    with pytest.raises(model.AutomationError, match=why):
        model.validate_action(_command("p1", inputs.pop("argv"), mapped, **inputs))


def test_a_mapped_field_must_fill_a_slot():
    with pytest.raises(model.AutomationError, match="fills no argument"):
        model.validate_action(
            _command("p1", [{"value": "make"}], {"x": {"path": "a", "type": "text"}})
        )


@pytest.mark.parametrize(
    "script, why",
    [
        ("/etc/passwd", "never absolute"),
        ("../elsewhere/evil.sh", "no .."),
        (".hidden/x.sh", "hidden"),
    ],
)
def test_run_script_path_is_relative_and_inside(script, why):
    with pytest.raises(model.AutomationError, match=why):
        model.validate_action(_scripted("p1", script))


@pytest.mark.parametrize("program", ["bash", "sh", "/bin/sh", "env", "xargs", "sudo", "ssh"])
def test_a_shell_or_a_launcher_is_never_the_program(shop, program):
    with pytest.raises(model.AutomationError, match="runs other commands|is not found"):
        routes._create(_config(_command(shop["id"], [{"value": program}, {"value": "x"}])))


def test_a_fixed_credential_is_refused_at_save(shop):
    token = "ghp_R4nd0mT0k3nAbCdEfGhIjKlMnOpQrStUv12"
    with pytest.raises(model.AutomationError, match="never goes into a command as a value"):
        routes._create(_config(_echo(shop, {"value": token})))
    with pytest.raises(model.AutomationError, match="never goes into a command as a value"):
        routes._create(_config(_echo(shop, env=[{"name": "TOKEN", "value": token}])))


# ---- no shell, anywhere -------------------------------------------------------------------------


_RUN_LOCAL_FILES = [run_local, run_command, run_script]


def test_no_shell_layer_in_the_run_local_code():
    """The CI grep (`pr-validate.yml`) for the run-local modules, plus the call graph: every
    subprocess call takes a literal LIST and never enables a shell."""
    for mod in _RUN_LOCAL_FILES:
        src = inspect.getsource(mod)
        assert "shell=" not in src and "os.system" not in src and "os.popen" not in src
        assert "bash -c" not in src and "sh -c" not in src
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name not in ("system", "popen", "spawnl", "spawnlp", "execl", "execlp"), name
            if name in ("Popen", "run", "call", "check_call", "check_output"):
                base = fn.value if isinstance(fn, ast.Attribute) else None
                if not (isinstance(base, ast.Name) and base.id == "subprocess"):
                    continue
                assert all(kw.arg != "shell" for kw in node.keywords), ast.dump(node)
                first = node.args[0]
                # A literal list, or a name bound to one (`wrapped` from `scopedspawn.wrap`).
                assert isinstance(first, ast.List | ast.Name), ast.dump(first)


def test_the_spawn_takes_a_list_argv_through_containment_and_env_i(shop, monkeypatch):
    seen: list = []
    real = run_local.subprocess.Popen

    def spy(args, **kw):
        seen.append((args, kw))
        return real(args, **kw)

    monkeypatch.setattr(run_local.subprocess, "Popen", spy)
    aid = _made(_config(_echo(shop, {"value": "a b"})))
    assert _execute(_manual_run(aid))["run"]["outcome"] == "ok"
    ((args, kw),) = seen
    assert isinstance(args, list) and all(isinstance(a, str) for a in args)
    assert "shell" not in kw and kw["start_new_session"] is True
    assert args[:2] == [run_local.ENV_BIN, "-i"]
    assert args[-2:] == ["echo.py", "a b"]


# ---- delivery values ----------------------------------------------------------------------------


INJECTIONS = ["; rm -rf /", "$(touch /tmp/pwned-by-p3)", "`id`", "a b && c", "x'y\"z", "*"]


def test_a_delivered_value_arrives_verbatim_as_one_argument(shop, spawned):
    action = _echo(
        shop,
        {"value": "--"},
        {"from": "title"},
        {"value": "fixed"},
        mapped={"title": {"path": "pull_request.title", "type": "text"}},
    )
    aid, wid = _hooked(shop, action)
    _deliver(aid, wid, {**PR, "pull_request": {**PR["pull_request"], "title": "; rm -rf /"}})
    res = _execute(spawned[0])
    assert res["run"]["outcome"] == "ok", res["run"]
    assert _echoed(aid)["argv"] == ["--", "; rm -rf /", "fixed"]


@pytest.mark.parametrize("value", INJECTIONS)
def test_any_value_is_one_argument_and_one_variable_never_a_command(shop, spawned, value):
    """Past the delivery's own typing (which would refuse most of these), the executor still
    passes the value as exactly one argv element and one variable, never interpreted."""
    action = _echo(
        shop,
        {"value": "--"},
        {"from": "title"},
        mapped={"title": {"path": "pull_request.title", "type": "text"}},
        env=[{"name": "PR_TITLE", "from": "title"}],
    )
    aid, wid = _hooked(shop, action)
    _deliver(aid, wid, PR)
    run_ = spawned[0]
    run_["inputs"] = {**(run_.get("inputs") or {}), "delivered": {"title": value}}
    res = _execute(run_)
    assert res["run"]["outcome"] == "ok", res["run"]
    got = _echoed(aid)
    assert got["argv"] == ["--", value]
    assert got["env"]["PR_TITLE"] == value
    assert not Path("/tmp/pwned-by-p3").exists()


def test_a_delivered_value_that_reads_as_an_option_is_refused_without_a_fixed_double_dash(
    shop, spawned
):
    action = _echo(
        shop, {"from": "title"}, mapped={"title": {"path": "pull_request.title", "type": "text"}}
    )
    aid, wid = _hooked(shop, action)
    _deliver(aid, wid, {**PR, "pull_request": {**PR["pull_request"], "title": "-rf"}})
    res = _execute(spawned[0])
    assert res["run"]["outcome"] == "refused" and "read as an option" in res["run"]["reason"]
    assert "output" not in _recorded(aid)


def test_a_delivery_never_fills_the_program_or_the_script():
    m = {"x": {"path": "a", "type": "text"}}
    with pytest.raises(model.AutomationError, match="always fixed"):
        model.validate_action(_command("p1", [{"from": "x"}, {"value": "y"}], m))
    with pytest.raises(model.AutomationError):
        model.validate_action(_scripted("p1", "scripts/build.sh", mapped={"script": m["x"]}))


# ---- environment and output ---------------------------------------------------------------------


def test_the_environment_is_clean(shop):
    aid = _made(_config(_echo(shop, env=[{"name": "MODE", "value": "fast"}])))
    assert _execute(_manual_run(aid))["run"]["outcome"] == "ok"
    env = _echoed(aid)["env"]
    import pwd

    home = pwd.getpwuid(os.getuid()).pw_dir
    assert env == {
        "PATH": run_local.CLEAN_PATH,
        "HOME": home,
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "MODE": "fast",
    }
    assert home != str(shop["tmp"])  # the app's own HOME is not passed on


def test_output_is_capped_and_redacted(shop):
    _script(
        shop["root"] / "scripts" / "noisy.py",
        "#!/usr/bin/python3\nimport sys\n"
        "print('token ghp_R4nd0mT0k3nAbCdEfGhIjKlMnOpQrStUv12')\nprint('fine line')\n"
        "sys.stdout.write('x' * 200000)\nsys.stderr.write('e' * 100000)\n",
    )
    aid = _made(_config(_scripted(shop["id"], "scripts/noisy.py")))
    assert _execute(_manual_run(aid))["run"]["outcome"] == "ok"
    out = _recorded(aid)["output"]
    assert "ghp_" not in out["stdout"] and "fine line" in out["stdout"]
    assert len(out["stdout"].encode()) < run_local.OUTPUT_CAP + 200
    assert out["stdout"].endswith("more bytes were not kept]")
    assert len(out["stderr"].encode()) < run_local.OUTPUT_CAP + 200
    assert "were not kept]" in out["stderr"]


def test_typed_fields_are_read_from_the_last_json_line(shop):
    _script(
        shop["root"] / "scripts" / "report.py",
        "#!/usr/bin/python3\nimport json\nprint('building…')\n"
        "print(json.dumps({'build_id': 'b-42', 'count': 3, 'green': True, 'extra': 1, "
        "'wrong': 'nope'}))\n",
    )
    outputs = {"build_id": "text", "count": "number", "green": "yes_no", "wrong": "number"}
    aid = _made(_config(_scripted(shop["id"], "scripts/report.py", outputs=outputs)))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "ok"
    assert "wrong is not a number" in res["run"]["reason"]
    out = _recorded(aid)["output"]
    assert out["exit_code"] == 0
    assert out["fields"] == {"build_id": "b-42", "count": 3, "green": True}


@pytest.mark.parametrize(
    "code, outcome, words", [(0, "ok", "exited 0"), (3, "failed", "exited with code 3")]
)
def test_exit_codes_map_to_outcomes(shop, code, outcome, words):
    _script(shop["root"] / "scripts" / "exit.sh", f"#!/bin/sh\nexit {code}\n")
    aid = _made(_config(_scripted(shop["id"], "scripts/exit.sh")))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == outcome and words in res["run"]["reason"]
    assert _recorded(aid)["output"]["exit_code"] == code


# ---- where it runs ------------------------------------------------------------------------------


def test_it_runs_in_the_project_folder(shop):
    here = shop["root"].resolve()
    _script(
        shop["root"] / "scripts" / "pwd.sh", f'#!/bin/sh\n[ "$(pwd)" = "{here}" ] && echo SAME\n'
    )
    aid = _made(_config(_scripted(shop["id"], "scripts/pwd.sh")))
    assert _execute(_manual_run(aid))["run"]["outcome"] == "ok"
    assert _recorded(aid)["output"]["stdout"].strip() == "SAME"


def test_an_excluded_or_out_of_scope_folder_is_refused(shop):
    from agent_sessions import prefs

    marker = shop["tmp"] / "ran"
    _script(shop["root"] / "scripts" / "mark.sh", f"#!/bin/sh\ntouch {marker}\n")
    aid = _made(_config(_scripted(shop["id"], "scripts/mark.sh")))
    prefs.set_folder_exclusions([str(shop["root"])])
    with pytest.raises(model.AutomationError, match="outside your project folders"):
        routes._create(_config(_scripted(shop["id"], "scripts/mark.sh")))
    res = _execute(_manual_run(aid))
    assert (
        res["run"]["outcome"] == "refused"
        and "outside your project folders" in res["run"]["reason"]
    )
    assert not marker.exists()


def test_a_symlink_escape_is_refused(shop):
    link = shop["root"] / "scripts" / "out.sh"
    link.symlink_to(shop["other"] / "evil.sh")
    with pytest.raises(model.AutomationError, match="symbolic link"):
        routes._create(_config(_scripted(shop["id"], "scripts/out.sh")))
    (shop["root"] / "linked").symlink_to(shop["other"])
    with pytest.raises(model.AutomationError, match="outside the project's folder"):
        routes._create(_config(_scripted(shop["id"], "linked/evil.sh")))


def test_a_script_swapped_for_a_link_out_after_approval_is_refused(shop):
    marker = shop["tmp"] / "ran"
    _script(shop["other"] / "evil.sh", f"#!/bin/sh\ntouch {marker}\n")
    aid = _made(_config(_scripted(shop["id"])))
    target = shop["root"] / "scripts" / "build.sh"
    target.unlink()
    target.symlink_to(shop["other"] / "evil.sh")
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "approval again" in res["run"]["reason"]
    assert not marker.exists()
    assert store.get(aid)["needs_reapproval"]


# ---- containment fails closed -------------------------------------------------------------------


@pytest.mark.parametrize("how", ["unavailable", "disabled"])
def test_no_containment_refuses_the_run_and_nothing_runs(shop, monkeypatch, how):
    marker = shop["tmp"] / "ran"
    _script(shop["root"] / "scripts" / "mark.sh", f"#!/bin/sh\ntouch {marker}\n")
    aid = _made(_config(_scripted(shop["id"], "scripts/mark.sh")))
    if how == "disabled":
        monkeypatch.setattr(scopedspawn, "enabled", lambda: False)
    else:
        monkeypatch.setattr(scopedspawn, "available", lambda: False)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "never runs uncontained" in res["run"]["reason"]
    assert not marker.exists()


def test_a_wrap_that_falls_back_to_the_bare_argv_is_refused(shop, monkeypatch):
    """`scopedspawn.wrap` returns the bare argv (no unit) when its ladder falls back: refused."""
    marker = shop["tmp"] / "ran"
    _script(shop["root"] / "scripts" / "mark.sh", f"#!/bin/sh\ntouch {marker}\n")
    aid = _made(_config(_scripted(shop["id"], "scripts/mark.sh")))
    monkeypatch.setattr(scopedspawn, "wrap", lambda argv, **kw: (argv, None))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "uncontained" in res["run"]["reason"]
    assert not marker.exists()


def test_no_containment_refuses_the_save(shop, monkeypatch):
    monkeypatch.setattr(scopedspawn, "enabled", lambda: False)
    with pytest.raises(model.AutomationError, match="never runs uncontained"):
        routes._create(_config(_scripted(shop["id"])))


# ---- pins: drift and the launch-bound check -----------------------------------------------------


def test_an_edited_script_needs_approval_again_and_does_not_run(shop):
    marker = shop["tmp"] / "ran"
    path = shop["root"] / "scripts" / "build.sh"
    aid = _made(_config(_scripted(shop["id"])))
    _script(path, f"#!/bin/sh\ntouch {marker}\n")
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "approval again" in res["run"]["reason"]
    assert "scripts/build.sh changed since you approved it" in res["run"]["reason"]
    assert store.get(aid)["needs_reapproval"] and not marker.exists()


def test_a_script_swapped_between_the_check_and_the_exec_runs_the_checked_bytes(shop, monkeypatch):
    """The swap lands at the last instant before the spawn (after every check): the run still
    executes the bytes that were checked (its sealed copy), never the new file."""
    path = shop["root"] / "scripts" / "who.sh"
    _script(path, "#!/bin/sh\necho ORIGINAL\n")
    aid = _made(_config(_scripted(shop["id"], "scripts/who.sh")))
    marker = shop["tmp"] / "swapped-ran"

    def swap(_launch):
        fresh = shop["root"] / "scripts" / "who.new"
        _script(fresh, f"#!/bin/sh\necho SWAPPED\ntouch {marker}\n")
        os.replace(fresh, path)  # a different inode at the same name
        # ... and the old inode rewritten in place, for good measure, is irrelevant: the copy
        # is sealed.

    monkeypatch.setattr(run_local, "_before_spawn", swap)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "ok", res["run"]
    assert _recorded(aid)["output"]["stdout"].strip() == "ORIGINAL"
    assert not marker.exists()


def test_a_script_rewritten_in_place_after_the_check_still_runs_the_checked_bytes(
    shop, monkeypatch
):
    path = shop["root"] / "scripts" / "who.sh"
    _script(path, "#!/bin/sh\necho ORIGINAL\n")
    aid = _made(_config(_scripted(shop["id"], "scripts/who.sh")))
    monkeypatch.setattr(
        run_local, "_before_spawn", lambda _l: path.write_text("#!/bin/sh\necho SWAPPED\n")
    )
    assert _execute(_manual_run(aid))["run"]["outcome"] == "ok"
    assert _recorded(aid)["output"]["stdout"].strip() == "ORIGINAL"


def test_a_changed_interpreter_needs_approval_again(shop, tmp_path, monkeypatch):
    interp = tmp_path / "bin" / "myinterp"
    interp.parent.mkdir()
    interp.write_bytes(Path("/bin/sh").read_bytes())
    interp.chmod(0o755)
    _script(shop["root"] / "scripts" / "own.sh", f"#!{interp}\necho via own\n")
    aid = _made(_config(_scripted(shop["id"], "scripts/own.sh")))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "ok", res["run"]
    with open(interp, "ab") as f:
        f.write(b"\0")
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "approval again" in res["run"]["reason"]
    assert f"the interpreter {interp} changed" in res["run"]["reason"]
    assert store.get(aid)["needs_reapproval"]


def test_an_env_interpreter_pins_what_it_resolves_to(shop):
    _script(shop["root"] / "scripts" / "e.py", "#!/usr/bin/env python3\nprint('via env')\n")
    action = model.validate_action(_scripted(shop["id"], "scripts/e.py"))
    p = run_script.pins(action)
    assert [i["path"] for i in p["interpreter"]] == ["/usr/bin/env", "python3"]
    assert all(len(i["sha256"]) == 64 for i in p["interpreter"])


def test_a_changed_program_needs_approval_again(shop, tmp_path):
    prog = tmp_path / "bin" / "tool"
    prog.parent.mkdir()
    prog.write_bytes(Path("/usr/bin/true").resolve().read_bytes())
    prog.chmod(0o755)
    aid = _made(_config(_command(shop["id"], [{"value": str(prog)}])))
    assert _execute(_manual_run(aid))["run"]["outcome"] == "ok"
    with open(prog, "ab") as f:
        f.write(b"\0")
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "approval again" in res["run"]["reason"]
    assert f"the program {prog} changed" in res["run"]["reason"]


def test_a_program_replaced_during_the_launch_is_refused(shop, tmp_path, monkeypatch):
    """The hash is taken through a descriptor; right before the spawn the path must still name
    that very file. A replacement in between is refused and asks again."""
    prog = tmp_path / "bin" / "tool"
    prog.parent.mkdir()
    prog.write_bytes(Path("/usr/bin/true").resolve().read_bytes())
    prog.chmod(0o755)
    aid = _made(_config(_command(shop["id"], [{"value": str(prog)}])))
    real_hash = run_local._hash_fd

    def hash_then_swap(fd, cap):
        out = real_hash(fd, cap)
        fresh = tmp_path / "bin" / "tool.new"
        fresh.write_bytes(prog.read_bytes())
        fresh.chmod(0o755)
        os.replace(fresh, prog)
        return out

    monkeypatch.setattr(run_local, "_hash_fd", hash_then_swap)
    res = _execute(_manual_run(aid))
    assert (
        res["run"]["outcome"] == "refused" and "replaced during the launch" in res["run"]["reason"]
    )


def test_a_moved_project_needs_approval_again(shop, tmp_path):
    from agent_sessions import projects

    aid = _made(_config(_scripted(shop["id"])))
    new = tmp_path / "shop2" / "scripts"
    new.mkdir(parents=True)
    _script(new / "build.sh", "#!/bin/sh\necho building\n")
    projects.update(
        shop["id"], folders=[str(tmp_path / "shop2")], default_folder=str(tmp_path / "shop2")
    )
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "approval again" in res["run"]["reason"]


# ---- the process lifecycle ----------------------------------------------------------------------


def test_the_time_limit_kills_the_whole_tree_and_is_recorded(shop, monkeypatch, contained):
    monkeypatch.setattr(run_local, "MINUTE_S", 1.5)
    pids = shop["root"] / "pids"
    aid = _made(
        _config(
            _scripted(
                shop["id"],
                "scripts/tree.py",
                args=[{"value": "pids"}, {"value": "stay"}],
                timeout_minutes=1,
            )
        )
    )
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "failed"
    assert "stopped after 1 minute" in res["run"]["reason"]
    tree = [int(p) for p in pids.read_text().split()]
    assert len(tree) == 3 and _gone(tree), tree
    assert contained["scope_kill"] == [FAKE_UNIT]
    out = _recorded(aid)["output"]
    assert out["ended"] == "timeout" and out["exit_code"] is None


def test_what_a_finished_program_left_running_is_killed(shop, contained):
    _script(
        shop["root"] / "scripts" / "bg.py",
        "#!/usr/bin/python3\nimport subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\nprint(p.pid)\n",
    )
    aid = _made(_config(_scripted(shop["id"], "scripts/bg.py")))
    t0 = time.monotonic()
    assert _execute(_manual_run(aid))["run"]["outcome"] == "ok"
    assert time.monotonic() - t0 < 20
    left = int(_recorded(aid)["output"]["stdout"].strip())
    assert _gone([left])


def test_a_cancel_kills_the_tree_and_a_disable_meanwhile_reports_in_flight(shop, monkeypatch):
    monkeypatch.setattr(run_local, "MINUTE_S", 30.0)
    pids = shop["root"] / "pids"
    aid = _made(
        _config(
            _scripted(shop["id"], "scripts/tree.py", args=[{"value": "pids"}, {"value": "stay"}])
        )
    )
    run_ = _manual_run(aid)

    async def go():
        task = asyncio.ensure_future(automation_runner.execute(run_, registry=None))
        while not pids.exists():
            await asyncio.sleep(0.02)
        # The command is running and holds the effect lock: the disable commits after its wait
        # and says a run in progress may still complete.
        disabled = await asyncio.to_thread(routes._simple, aid, "disable")
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return disabled

    t0 = time.monotonic()
    disabled = asyncio.run(go())
    assert time.monotonic() - t0 < 20
    assert disabled["in_flight"] is True
    tree = [int(p) for p in pids.read_text().split()]
    assert _gone(tree), tree
    (row,) = _runs(aid)
    assert row["outcome"] == "failed" and "cancelled" in row["reason"]


def test_a_disable_before_the_launch_runs_nothing_and_is_not_in_flight(shop, monkeypatch):
    marker = shop["tmp"] / "ran"
    _script(shop["root"] / "scripts" / "mark.sh", f"#!/bin/sh\ntouch {marker}\n")
    aid = _made(_config(_scripted(shop["id"], "scripts/mark.sh")))
    real = store.set_inputs
    acks: list = []

    def then_disabled(rid, inputs):
        real(rid, inputs)
        if "output" not in inputs and not acks:
            acks.append(routes._simple(aid, "disable"))

    monkeypatch.setattr(store, "set_inputs", then_disabled)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "stopped"
    assert not marker.exists()


def test_an_unknown_outcome_after_a_crash_is_never_replayed(shop, monkeypatch):
    marker = shop["tmp"] / "count"
    _script(shop["root"] / "scripts" / "count.sh", f"#!/bin/sh\necho x >> {marker}\n")
    aid = _made(_config(_scripted(shop["id"], "scripts/count.sh")))
    run_ = _manual_run(aid)
    with store._tx() as con:  # the run was dispatching when its owner died
        con.execute(
            "UPDATE automation_runs SET state='dispatching', owner=? WHERE id=?",
            ("dead-owner:1", run_["id"]),
        )
    monkeypatch.setattr(store, "owner_alive", lambda owner: False)
    assert store.recover_interrupted() == [run_["id"]]
    (row,) = _runs(aid)
    assert row["outcome"] == "interrupted"
    assert not marker.exists()  # recovery records; it never runs the command again


@pytest.fixture
def real_scope(monkeypatch, shop):
    """A REAL transient scope, when this host's user manager answers. Never on a runner without
    one (skipped)."""
    runtime = f"/run/user/{os.getuid()}"
    if not os.path.exists(f"{runtime}/bus"):
        pytest.skip("no user systemd here")
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    ss = scopedspawn

    monkeypatch.setattr(ss, "enabled", _REAL_ENABLED)
    monkeypatch.setattr(ss, "available", _REAL_AVAILABLE)
    monkeypatch.setattr(ss, "wrap", _REAL_WRAP)
    monkeypatch.setattr(run_local, "_scope_kill", _REAL_SCOPE_KILL)
    ss.reset_cache_for_tests()
    monkeypatch.delenv("AGENT_SESSIONS_SESSION_SCOPES", raising=False)
    if not ss.available():
        pytest.skip("systemd-run --user can't make a scope here")
    yield
    ss.reset_cache_for_tests()


_REAL_ENABLED = scopedspawn.enabled
_REAL_AVAILABLE = scopedspawn.available
_REAL_WRAP = scopedspawn.wrap
_REAL_SCOPE_KILL = run_local._scope_kill


def test_a_real_scope_kills_a_descendant_that_left_the_process_group(shop, real_scope, monkeypatch):
    monkeypatch.setattr(run_local, "MINUTE_S", 2.0)
    pids = shop["root"] / "pids"
    aid = _made(
        _config(
            _scripted(
                shop["id"],
                "scripts/tree.py",
                args=[{"value": "pids"}, {"value": "escape"}],
                timeout_minutes=1,
            )
        )
    )
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "failed" and "stopped after 1 minute" in res["run"]["reason"]
    tree = [int(p) for p in pids.read_text().split()]
    assert _gone(tree), tree


# ---- the consent --------------------------------------------------------------------------------


def test_the_consent_states_the_argv_digest_folder_user_and_honesty_lines(shop):
    import pwd

    action = _echo(
        shop,
        {"value": "--"},
        {"from": "title"},
        mapped={"title": {"path": "pull_request.title", "type": "text"}},
        env=[{"name": "PR", "from": "title"}],
    )
    pub, _ = webhooks.create("ci")
    out = routes._create(_config(action, trigger={"kind": "webhook", "webhook_id": pub["id"]}))
    lines = "\n".join(routes.public(store.get(out["id"]))["scope_lines"])
    prog = os.path.realpath("/usr/bin/python3")
    assert f"The program is {prog} (sha256 " in lines
    assert '"python3", ' in lines and "<the delivery's pull_request.title, as short text>" in lines
    assert str(shop["root"].resolve()) in lines
    assert pwd.getpwuid(os.getuid()).pw_name in lines
    assert "Never through a shell" in lines
    assert "PR (<the delivery's pull_request.title, as short text>)" in lines
    assert "Stops after 5 minutes" in lines and "64 KB" in lines
    assert "Secrets never go in as values" in lines
    assert "asks for your approval again" in lines
    assert "neither is a filesystem or network sandbox" in lines


def test_the_script_consent_shows_its_full_digest(shop):
    out = routes._create(_config(_scripted(shop["id"])))
    pub = routes.public(store.get(out["id"]))
    lines = "\n".join(pub["scope_lines"])
    import hashlib

    digest = hashlib.sha256((shop["root"] / "scripts" / "build.sh").read_bytes()).hexdigest()
    assert f"(sha256 {digest})" in lines
    assert "Its interpreter: " in lines and "a sealed copy" in lines


# ---- the wizard's script picker -----------------------------------------------------------------


def test_the_picker_lists_scripts_inside_the_project_only(shop):
    (shop["root"] / "README.md").write_text("# not a script\n")
    (shop["root"] / ".hidden").mkdir()
    _script(shop["root"] / ".hidden" / "x.sh", "#!/bin/sh\n")
    (shop["root"] / "node_modules").mkdir()
    _script(shop["root"] / "node_modules" / "y.sh", "#!/bin/sh\n")
    (shop["root"] / "scripts" / "out.sh").symlink_to(shop["other"] / "evil.sh")
    got = run_script.list_scripts(shop["id"])
    assert got["folder"] == str(shop["root"].resolve()) and not got["truncated"]
    assert [s["path"] for s in got["scripts"]] == ["scripts/build.sh", "scripts/tree.py"]


def test_the_picker_never_lists_an_excluded_folder(shop):
    from agent_sessions import prefs

    prefs.set_folder_exclusions([str(shop["root"])])
    with pytest.raises(model.AutomationError, match="outside your project folders"):
        run_script.list_scripts(shop["id"])


def test_the_picker_route_needs_a_login_and_answers_no_store(shop):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from agent_sessions.routes import actions as action_routes

    def refused():
        raise HTTPException(401)

    app = FastAPI()
    action_routes.register(app, logged_in=refused)
    url = f"{action_routes.SCRIPTS_API}?project_id={shop['id']}"
    assert TestClient(app).get(url).status_code == 401
    app = FastAPI()
    action_routes.register(app, logged_in=lambda: "op")
    r = TestClient(app).get(url)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert [s["path"] for s in r.json()["scripts"]] == ["scripts/build.sh", "scripts/tree.py"]
    assert TestClient(app).get(f"{action_routes.SCRIPTS_API}?project_id=nope").status_code == 404


# ---- the browser fixture is the server's own wording --------------------------------------------

CONSENT_FIXTURE = Path(__file__).resolve().parents[1] / "web/e2e/fixtures/run-local-consent.json"


def _fixture_scope(action: dict, plugin_pins: dict) -> dict:
    cfg = model.validate_config(
        _config(action, trigger={"kind": "webhook", "webhook_id": "0a1b2c3d4e5f6a7b"})
    )
    base = {"cwd": {"path": "/home/op/shop", "real": "/home/op/shop", "dev": 1, "ino": 2},
            "run_as": {"uid": 1000, "user": "op", "home": "/home/op"}}  # fmt: skip
    pins = {"template": None, "checklist": None, "plugin": {**base, **plugin_pins}}
    return model.scope_of(cfg, pins)


def test_the_committed_consent_fixture_is_what_the_server_sends():
    """`web/e2e/fixtures/run-local-consent.json` feeds the browser specs' consent and drift views:
    the plugins' own consent lines and drift words for the configs the specs save, with fixed
    pins. Regenerate deliberately with `AGENT_SESSIONS_WRITE_FIXTURES=1`."""
    pr = {"path": "pull_request.number", "type": "number"}
    bash = [{"path": "/usr/bin/env", "real": "/usr/bin/env", "sha256": "1" * 64},
            {"path": "bash", "real": "/usr/bin/bash", "sha256": "2" * 64}]  # fmt: skip
    script = _fixture_scope(
        _scripted(
            "p1",
            "scripts/deploy-preview.sh",
            args=[{"value": "--"}, {"from": "arg_2"}],
            mapped={"arg_2": pr, "env_pr_number": pr},
            env=[{"name": "PR_NUMBER", "from": "env_pr_number"}],
        ),
        {"script": {"path": "scripts/deploy-preview.sh",
                    "real": "/home/op/shop/scripts/deploy-preview.sh",
                    "sha256": "3f9a" + "0" * 56 + "c21e"},
         "interpreter": bash},
    )  # fmt: skip
    command = _fixture_scope(
        _command(
            "p1",
            [{"value": "make"}, {"value": "-C", "option": True}, {"value": "site"}],
            env=[{"name": "MODE", "value": "fast"}],
            timeout_minutes=10,
        ),
        {"program": {"real": "/usr/bin/make", "sha256": "4" * 64}, "interpreter": []},
    )
    docs = "/home/op/docs/scripts/build-docs.sh"
    old = {"script": {"path": "scripts/build-docs.sh", "real": docs,
                      "sha256": "71c0" + "0" * 56 + "9ab2"}}  # fmt: skip
    new = {"script": {**old["script"], "sha256": "08d4" + "0" * 56 + "e3f1"}}
    doc = {
        "run_script": plugins.lines(script),
        "run_command": plugins.lines(command),
        "drift_reason": run_script.drift(old, new),
    }
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if os.environ.get("AGENT_SESSIONS_WRITE_FIXTURES") == "1":
        CONSENT_FIXTURE.write_text(text)
    assert CONSENT_FIXTURE.read_text() == text, (
        "web/e2e/fixtures/run-local-consent.json is not what the server says; regenerate it with "
        "AGENT_SESSIONS_WRITE_FIXTURES=1 and review the diff"
    )


# ---- #2279 review: a delivery never supplies code to an interpreter -----------------------------

TITLE = {"title": {"path": "pull_request.title", "type": "text"}}
PWNED = "/tmp/pwned-by-p3-review"


def _forged_run(aid: str, argv: list) -> dict:
    """A run whose approved config was FORGED in the store after the save's checks."""
    run_ = _manual_run(aid)
    run_ = copy.deepcopy(run_)
    run_["scope"]["config"]["action"]["inputs"]["argv"] = argv
    return run_


def test_the_review_config_python_dash_c_from_the_delivery_is_refused_at_save(shop):
    """Hermes on #2279: `[python3, -c, {from: code}]` ran a delivered title as Python."""
    action = _command(shop["id"], [{"value": "python3"}, {"value": "-c"}, {"from": "title"}], TITLE)
    with pytest.raises(model.AutomationError, match="always fixed|program text"):
        model.validate_action(action)


def test_the_review_config_forged_into_the_store_is_refused_at_run(shop, spawned):
    aid, wid = _hooked(shop, _echo(shop, {"value": "--"}, {"from": "title"}, mapped=TITLE))
    _deliver(aid, wid, {**PR, "pull_request": {**PR["pull_request"], "title": "print(424242)"}})
    run_ = copy.deepcopy(spawned[0])
    run_["scope"]["config"]["action"]["inputs"]["argv"] = [
        {"value": "python3"},
        {"value": "-c"},
        {"from": "title"},
    ]
    res = _execute(run_)
    assert res["run"]["outcome"] == "refused", res["run"]
    assert "output" not in _recorded(aid)


@pytest.mark.parametrize(
    "argv",
    [
        [{"value": "python3"}, {"from": "title"}],
        [{"value": "/usr/bin/python3.12"}, {"from": "title"}],
        [{"value": "awk"}, {"from": "title"}],
        [{"value": "node"}, {"from": "title"}],
        [{"value": "perl"}, {"value": "-ne"}, {"from": "title"}],
        [{"value": "jq"}, {"value": "--arg"}, {"value": "n"}, {"value": "v"}, {"from": "title"}],
        [{"value": "python3"}, {"value": "-X"}, {"value": "dev"}, {"from": "title"}],
        [{"value": "deno"}, {"value": "run"}, {"from": "title"}],
        [{"value": "sed"}, {"value": "-n"}, {"value": "p"}, {"from": "title"}],
    ],
)
def test_a_delivery_never_fills_a_code_taking_position(argv):
    with pytest.raises(model.AutomationError):
        model.validate_action(_command("p1", argv, TITLE))


def test_a_fixed_dash_element_without_the_option_flag_still_keeps_the_next_slot_fixed():
    with pytest.raises(model.AutomationError, match="always fixed"):
        model.validate_action(
            _command("p1", [{"value": "grep"}, {"value": "-e"}, {"from": "title"}], TITLE)
        )


@pytest.mark.parametrize(
    "argv",
    [
        [{"value": "python3"}, {"value": "-c"}, {"value": "print(1)"}, {"from": "title"}],
        [{"value": "python3"}, {"value": "echo.py"}, {"from": "title"}],
        [
            {"value": "awk"},
            {"value": "-v"},
            {"value": "x=1"},
            {"value": "{print}"},
            {"from": "title"},
        ],
        [{"value": "grep"}, {"value": "--"}, {"from": "title"}],
    ],
)
def test_a_delivery_after_the_operators_program_text_is_allowed(argv):
    model.validate_action(_command("p1", argv, TITLE))


def test_python_dash_c_fixed_code_gets_the_value_as_argv_never_runs_it(shop, spawned):
    code = "import json, sys; print(json.dumps({'argv': sys.argv[1:], 'env': {}}))"
    action = _command(
        shop["id"],
        [{"value": "python3"}, {"value": "-c"}, {"value": code}, {"from": "title"}],
        TITLE,
    )
    aid, wid = _hooked(shop, action)
    evil = f"open('{PWNED}', 'w').write('x')"
    _deliver(aid, wid, {**PR, "pull_request": {**PR["pull_request"], "title": "x"}})
    run_ = spawned[0]
    run_["inputs"] = {**(run_.get("inputs") or {}), "delivered": {"title": evil}}
    res = _execute(run_)
    assert res["run"]["outcome"] == "ok", res["run"]
    assert _echoed(aid)["argv"] == [evil]
    assert not Path(PWNED).exists()


# ---- #2279 review: output parsing never loses the run's record ----------------------------------


@pytest.mark.parametrize(
    "last, why",
    [
        pytest.param('{"n": ' + "9" * 400 + "}", "n is not a number", id="huge-int"),
        pytest.param('{"n": 1e999}', "n is not a number", id="inf"),
        pytest.param('{"n": -1e999}', "n is not a number", id="minus-inf"),
        pytest.param('{"n": 9007199254740993}', "n is not a number", id="past-2-53"),
        pytest.param('{"n": ', "not valid JSON", id="malformed"),
        pytest.param('{"n": ' + "[" * 3000, "not valid JSON", id="deep-nesting"),
    ],
)
def test_odd_numbers_and_broken_json_leave_the_field_out_and_the_run_recorded(shop, last, why):
    _script(
        shop["root"] / "scripts" / "odd.py",
        f"#!/usr/bin/python3\nimport sys\nsys.stdout.write({last!r} + '\\n')\nsys.exit(3)\n",
    )
    aid = _made(_config(_scripted(shop["id"], "scripts/odd.py", outputs={"n": "number"})))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "failed" and "exited with code 3" in res["run"]["reason"]
    assert why in res["run"]["reason"]
    out = _recorded(aid)["output"]
    assert out["exit_code"] == 3 and out["fields"] == {}


def test_a_parse_crash_still_records_the_run(shop, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(run_local, "typed_fields", boom)
    aid = _made(_config(_scripted(shop["id"], outputs={"n": "number"})))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "ok", res["run"]
    out = _recorded(aid)["output"]
    assert out["exit_code"] == 0 and out["fields"] == {}
    assert "building" in out["stdout"]


# ---- #2279 review: the picker honours folder_exclusions inside the project ----------------------


def test_the_picker_skips_an_excluded_subfolder(shop):
    from agent_sessions import prefs

    (shop["root"] / "private").mkdir()
    _script(shop["root"] / "private" / "internal.sh", "#!/bin/sh\n")
    prefs.set_folder_exclusions([str(shop["root"] / "private")])
    got = [s["path"] for s in run_script.list_scripts(shop["id"])["scripts"]]
    assert "private/internal.sh" not in got and "scripts/build.sh" in got
