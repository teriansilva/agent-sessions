"""Run a script (#2248 P3): a script file inside a project's folder, executed directly (``#!``).

``inputs = {project_id, script, args?, env?, timeout_minutes?, outputs?}``:

* ``script`` — a path RELATIVE to the project's folder (no absolute path, ``~``, ``..`` or hidden
  segment), fixed at save. It must be a regular file (never a symbolic link) whose real path stays
  inside the folder, start with a ``#!`` line naming an absolute interpreter, and be at most
  1 MiB. ``#!/usr/bin/env NAME`` is allowed with exactly one plain name (no ``-S``).
* ``args`` — extra arguments after the script: ``{value, option?}`` or ``{from: field}``, as for
  Run a command (an option and the element after it are always fixed).
* ``env``, ``timeout_minutes``, ``outputs`` — as for Run a command.

**What runs is what was checked.** The script is read ONCE through a descriptor, hashed against the
pin, and copied into a sealed in-memory file (`run_local.sealed_copy`); the kernel executes THAT
copy (``/proc/self/fd/N``), so editing or swapping the file on disk after the check can't change a
single byte of this run. The interpreter chain is hashed again right before the launch. The script
sees ``$0`` as ``/proc/self/fd/N``; it starts in the project's folder, so paths relative to the
folder work. A changed script, interpreter or folder is a drift: the automation pauses and asks.
"""

from __future__ import annotations

from .. import automations as model
from . import publish_artifact
from . import run_command as rc
from . import run_local as rl

ID = "run_script"
LABEL = "Run a script"
WEBHOOK = True
MESSAGE = False
SECRET_REFUSAL = ""
TARGET_WORDS = {
    "project_id": "a different project",
    "folder": "the project's folder changed",
    "run_as": "a different user",
    "script": "a different script, or the script changed",
    "interpreter": "the script's interpreter changed",
    "args": "different arguments",
    "env": "different environment variables",
    "timeout_minutes": "a different time limit",
    "outputs": "different output fields",
}
_INPUTS = {"project_id", "script", "args", "env", "timeout_minutes", "outputs"}


# ---- validation and save ------------------------------------------------------------------------


def validate_inputs(inputs: dict, mapped: dict) -> dict:
    model._obj(inputs, "a run_script action's inputs", _INPUTS, {"project_id", "script"})
    pid = inputs["project_id"]
    if not isinstance(pid, str) or not pid or len(pid) > 200:
        raise model.AutomationError("project_id must be a project id")
    try:
        script = publish_artifact.normalize_path(inputs["script"])
    except model.AutomationError as e:
        raise model.AutomationError(str(e).replace("path", "script", 1)) from None
    args = rl.validate_elements(inputs.get("args", []), "args", mapped, first_fixed=False)
    env = rl.validate_env(inputs.get("env"), mapped)
    timeout = rl.validate_timeout(inputs.get("timeout_minutes"))
    outputs = rl.validate_outputs(inputs.get("outputs"))
    rl.check_mapping_used(mapped, args, env)
    return {
        "project_id": pid,
        "script": script,
        "args": args,
        "env": env,
        "timeout_minutes": timeout,
        "outputs": outputs,
    }


def mappable(_inputs: dict) -> None:
    return None  # checked by `validate_inputs`: exactly the names its argument slots use


def _script_pin(folder: str, rel: str, *, fresh: bool = False) -> tuple[dict, list[dict]]:
    real = rl.script_real(folder, rel)
    data, digest, _st = rl.read_checked(real, rel, rl.SCRIPT_MAX_BYTES)
    if rl.parse_shebang(data[: rl.SHEBANG_MAX + 3]) is None:
        raise rl.Refused(f"{rel} has no #! line naming its interpreter", drift=True)
    chain = rl.interpreter_chain(data[: rl.SHEBANG_MAX + 3], fresh=fresh)
    return {"path": rel, "real": real, "sha256": digest}, chain


def check_save(config: dict) -> None:
    inp = config["action"]["inputs"]
    why = rl.containment_problem()
    if why:
        raise model.AutomationError(why)
    try:
        cwd = rl.folder_pin(inp["project_id"])
    except model.PinsUnavailable as e:
        raise model.AutomationError(str(e), status=503) from None
    if cwd.get("missing"):
        raise model.AutomationError(str(cwd["missing"]))
    problem = rl.folder_problem(cwd.get("real"))
    if problem:
        raise model.AutomationError(problem)
    try:
        _script_pin(cwd["real"], inp["script"])
    except rl.Refused as r:
        raise model.AutomationError(r.reason) from None
    rl.check_fixed_values(inp["args"], inp["env"])


# ---- the wizard's script picker -----------------------------------------------------------------

#: What the picker walks: a bounded look, never a full index of a big tree.
LIST_MAX = 200
LIST_DEPTH = 4
LIST_SCAN_MAX = 5000
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", "dist", "build", "target", "vendor"})


def list_scripts(project_id: str) -> dict:
    """``{folder, scripts: [{path, size}], truncated}``: the files in the project's folder a Run a
    script action could pick (a ``#!`` first line, a regular file, not hidden, not a link, at most
    `run_local.SCRIPT_MAX_BYTES`). Read-only; the save checks the pick again. Raises
    `AutomationError` (a folder outside the boundary is never listed)."""
    try:
        cwd = rl.folder_pin(project_id)
    except model.PinsUnavailable as e:
        raise model.AutomationError(str(e), status=503) from None
    if cwd.get("missing"):
        raise model.AutomationError(str(cwd["missing"]), status=404)
    folder = cwd["real"]
    problem = rl.folder_problem(folder)
    if problem:
        raise model.AutomationError(problem, status=403)
    from .. import automation_runner as runner

    found: list[dict] = []
    scanned = 0
    truncated = False
    for here, dirs, files in rl.os.walk(folder, followlinks=False):
        rel_dir = rl.os.path.relpath(here, folder)
        depth = 0 if rel_dir == "." else rel_dir.count(rl.os.sep) + 1
        # An excluded subfolder (`folder_exclusions`) is never walked, let alone offered.
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".")
            and d not in _SKIP_DIRS
            and depth < LIST_DEPTH
            and runner.scope_ok(rl.os.path.join(here, d))
        )
        for name in sorted(files):
            if name.startswith("."):
                continue
            scanned += 1
            if scanned > LIST_SCAN_MAX or len(found) >= LIST_MAX:
                truncated = True
                break
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            item = rl.os.path.join(here, name)
            try:
                st = rl.os.lstat(item)
                if not rl.stat.S_ISREG(st.st_mode) or st.st_size > rl.SCRIPT_MAX_BYTES:
                    continue
                fd = rl.os.open(item, rl._O_READ | rl.os.O_NONBLOCK)
                try:
                    head = rl.os.pread(fd, 2, 0)
                finally:
                    rl.os.close(fd)
            except OSError:
                continue
            if head == b"#!" and runner.scope_ok(rl.os.path.realpath(item)):
                found.append({"path": rel, "size": st.st_size})
        if truncated:
            break
    return {"folder": folder, "scripts": found, "truncated": truncated}


# ---- pins, scope, consent -----------------------------------------------------------------------


def pins(action: dict) -> dict:
    inp = action["inputs"]
    cwd = rl.folder_pin(inp["project_id"])
    if cwd.get("missing"):
        return {"missing": str(cwd["missing"])}
    try:
        script, chain = _script_pin(cwd["real"], inp["script"])
    except rl.Refused as r:
        return {"missing": r.reason}
    return {"cwd": cwd, "run_as": rl.run_as(), "script": script, "interpreter": chain}


def drift(stored: dict, current: dict) -> str:
    if stored.get("cwd") != current.get("cwd"):
        return "the project's folder changed since you approved this automation"
    if stored.get("run_as") != current.get("run_as"):
        return "the user it runs as changed since you approved this automation"
    ss, cs = stored.get("script") or {}, current.get("script") or {}
    if ss.get("real") != cs.get("real"):
        return f"{ss.get('path')} is a different file now"
    if ss.get("sha256") != cs.get("sha256"):
        return rl.changed_words(str(ss.get("path")), ss, cs)
    if stored.get("interpreter") != current.get("interpreter"):
        return rl._interp_words(stored.get("interpreter") or [], current.get("interpreter") or [])
    return "what this script is bound to changed since you approved it"


def target(action: dict, pins_: dict) -> dict:
    inp = action["inputs"]
    p = pins_.get("plugin") or {}
    who = p.get("run_as") or {}
    return {
        "project_id": inp["project_id"],
        "folder": (p.get("cwd") or {}).get("real"),
        "run_as": {"uid": who.get("uid"), "user": who.get("user")},
        "script": p.get("script") or {"path": inp["script"]},
        "interpreter": p.get("interpreter") or [],
        "args": inp["args"],
        "env": inp["env"],
        "timeout_minutes": inp["timeout_minutes"],
        "outputs": inp["outputs"],
    }


def data(_action: dict) -> dict:
    return {}  # every input is in the target: any change is a new approval


def lines(scope: dict) -> list[str]:
    t = scope.get("target") or {}
    mapping = rc._mapping_of(scope)
    script = t.get("script")
    args = t.get("args") if isinstance(t.get("args"), list) else [t.get("args")]
    chain = (
        t.get("interpreter") if isinstance(t.get("interpreter"), list) else [t.get("interpreter")]
    )
    where = script.get("path") if isinstance(script, dict) else None
    out = [
        f"Runs the script {where} — {rl.pin_words(script)}",
        "With the arguments: [" + ", ".join(rl.show_element(e, mapping) for e in args) + "]",
    ]
    for i in chain:
        out.append(f"Its interpreter: {rl.pin_words(i)}")
    out.append(
        "It runs exactly the bytes checked at the launch (a sealed copy); if the script or its "
        "interpreter changes, or the project's folder moves, it asks for your approval again"
    )
    out.extend(rl.common_lines(t, mapping))
    return out


def fill(action: dict, delivered: dict) -> dict:
    return {**action, "inputs": {**action["inputs"], "delivered": dict(delivered)}}


# ---- the run ------------------------------------------------------------------------------------


def effect(run: dict, config: dict, pins_: dict) -> tuple[str, str]:
    """BLOCKING, under the effect lock, after the authority read."""
    inp = config["action"]["inputs"]
    pinned = pins_.get("plugin") or {}
    delivered = inp.get("delivered") or {}

    def launch_of() -> rl.Launch:
        folder = rc._checked_folder(inp["project_id"], pinned)
        pin = pinned.get("script") or {}
        real = rl.script_real(folder, inp["script"])
        if real != pin.get("real"):
            raise rl.Refused(f"{inp['script']} is a different file now", drift=True)
        data, digest, _st = rl.read_checked(real, inp["script"], rl.SCRIPT_MAX_BYTES)
        if digest != pin.get("sha256"):
            raise rl.Refused(rl.changed_words(inp["script"], pin, {"sha256": digest}), drift=True)
        rl.check_interpreters(data[: rl.SHEBANG_MAX + 3], pinned.get("interpreter") or [])
        args = rl.build_args(inp["args"], delivered)
        env = rl.build_env(inp["env"], delivered)
        # From here on, the bytes on disk no longer matter: the run executes this sealed copy.
        fd = rl.sealed_copy(data)
        return rl.Launch(
            payload=[f"/proc/self/fd/{fd}", *args],
            env=env,
            cwd=folder,
            shown=[real, *args],
            pass_fds=(fd,),
            owned_fds=[fd],
        )

    return rl.run_effect(
        run,
        launch_of=launch_of,
        record={
            "script": inp["script"],
            "script_sha256": (pinned.get("script") or {}).get("sha256"),
        },
        minutes=inp["timeout_minutes"],
        outputs=inp["outputs"],
    )
