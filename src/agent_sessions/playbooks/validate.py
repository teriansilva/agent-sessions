"""Validate a bundle snapshot into a normalized playbook (#1190).

**Unknown-field policy: reject, at every level.** An unknown key anywhere — manifest, flow, step,
checklist item, runbook frontmatter, template — makes the whole bundle invalid. A key added by a
later format may be a restriction, and a reader that skips it grants what the author meant to
withhold (the plugin manifest's rule, and its strict reader, `manifest._Reader`).

**Format policy.** `format` is an integer. Higher than this build's `schema.FORMAT` is refused as
"needs a newer BattleLab"; lower or missing is invalid until a migration exists for it.

**Symbolic checks at load, concrete checks at binding** (#1190 round 1). Here a probe argument is a
reference to a declared variable or to an ancestor step's declared output, and it is checked for
key presence (required keys present, no unknown key, optional keys may be absent — exactly as
`missions.validate_probe_args` treats them) and for type fit. The resolved VALUE is checked at
binding (#1191) by `missions.validate_probe_args`, the one authoritative function. A literal is
admitted only in an argument that chooses no target (`schema.LITERAL_ARGS`), and there it IS a
concrete value, so it goes through that argument's own contract from `missions.PROBE_ARG_SCHEMA`.

Nothing here opens a file, a socket or a process: it reads the in-memory `tree.Tree`.
"""

from __future__ import annotations

import contextlib
import tomllib
from collections.abc import Iterator
from typing import Any

from .. import missions, templates
from ..plugins.manifest import (
    ManifestError,
    _Reader,
    anchored_path,
    compile_id_pattern,
    relative_path,
)
from . import schema
from .errors import PlaybookFormatError
from .tree import Tree, check_segment

# --- small readers -------------------------------------------------------------------------------


@contextlib.contextmanager
def _in(where: str) -> Iterator[None]:
    """Prefix every error raised inside with the file it concerns."""
    try:
        yield
    except ManifestError as e:
        field = f"{where}: {e.field}" if e.field else where
        raise PlaybookFormatError(field, e.reason) from None


def _fail(where: str, reason: str) -> PlaybookFormatError:
    return PlaybookFormatError(where, reason)


def _prose(r: _Reader, key: str, *, max_len: int, required: bool = False) -> str:
    """Free text that may span lines: bounded, and free of control characters other than tab and
    newline (the template store's own rule, `templates._CONTROL_RE`)."""
    v = r.raw(key, ... if required else "")
    where = r._f(key)
    if not isinstance(v, str) or len(v) > max_len:
        raise _fail(where, f"must be a string of at most {max_len} characters")
    if templates._CONTROL_RE.search(v) or templates._SURROGATE_RE.search(v) or "\r" in v:
        raise _fail(where, "must not contain control characters")
    if required and not v.strip():
        raise _fail(where, "is required")
    return v


def _records(r: _Reader, key: str, *, max_items: int) -> list[_Reader]:
    """An array of tables, each wrapped in its own strict reader."""
    v = r.raw(key, [])
    where = r._f(key)
    if not isinstance(v, list):
        raise _fail(where, "must be a list of tables")
    if len(v) > max_items:
        raise _fail(where, f"has more than {max_items} entries")
    return [_Reader(item, f"{where}[{i}]") for i, item in enumerate(v)]


def _toml(tree: Tree, rel: str, *, max_bytes: int) -> dict:
    data = tree.files.get(rel)
    if data is None:
        raise _fail(rel, "is missing")
    if len(data) > max_bytes:
        raise _fail(rel, f"is larger than {max_bytes} bytes")
    try:
        return tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise _fail(rel, "is not UTF-8") from None
    except tomllib.TOMLDecodeError as e:
        raise _fail(rel, f"does not parse: {e}") from None
    except RecursionError:
        raise _fail(rel, "is nested too deeply") from None


def _format(doc: dict, where: str) -> dict:
    v = doc.get("format")
    if isinstance(v, bool) or not isinstance(v, int):
        raise _fail(f"{where}: format", f"must be the integer {schema.FORMAT}")
    if v > schema.FORMAT:
        raise _fail(f"{where}: format", f"format {v} needs a newer BattleLab")
    while v < schema.FORMAT:
        migrate = schema.MIGRATIONS.get(v)
        if migrate is None:
            raise _fail(f"{where}: format", f"format {v} is not supported")
        doc = migrate(doc)
        v += 1
    return doc


def _text_tokens(
    text: str, where: str, variables: dict[str, dict], *, strict_unknown: bool = True
) -> list[str]:
    """The declared, non-secret variables `text` interpolates, in order.

    A `secret` variable is REFUSED (#1096 §3: a secret is never substituted into anything a
    bundle renders — materials, runbooks, briefs, templates). `{{steps.…}}` is refused too: an
    observed output may only narrow a probe target, never become prose. With `strict_unknown`
    an undeclared `{{name}}` is an error; without it (a verbatim material) it is literal text.
    """
    if schema.STEP_TOKEN_RE.search(text):
        raise _fail(where, "a step output may only be referenced from a probe argument")
    names: list[str] = []
    for m in schema.VAR_TOKEN_RE.finditer(text):
        name = m.group(1)
        var = variables.get(name)
        if var is None:
            if strict_unknown:
                raise _fail(where, f"references an undeclared variable {{{{{name}}}}}")
            continue
        if var["kind"] == "secret":
            raise _fail(
                where,
                f"interpolates the secret variable {{{{{name}}}}} — a secret is never rendered "
                "into bundle text; reference it through a connection's credential instead",
            )
        if name not in names:
            names.append(name)
    return names


# --- manifest blocks -----------------------------------------------------------------------------


def _identity(r: _Reader) -> dict:
    t = r.table("identity")
    out = {
        "id": t.str("id", pattern=schema.PLAYBOOK_ID_RE),
        "name": t.str("name", max_len=schema.NAME_MAX),
        "publisher": t.str("publisher", max_len=schema.LABEL_MAX),
        "version": t.str("version", pattern=schema.VERSION_RE),
        "domain": t.str("domain", pattern=schema.DOMAIN_RE),
        "summary": t.str("summary", "", max_len=schema.SUMMARY_MAX),
    }
    t.done()
    return out


def _variable_default(v: dict, raw: Any, where: str) -> Any:
    vtype = v["type"]
    if vtype == "int":
        if isinstance(raw, bool) or not isinstance(raw, int) or abs(raw) > schema.INT_ABS_MAX:
            raise _fail(where, "must be an integer (not a boolean)")
        return raw
    if vtype == "bool":
        if not isinstance(raw, bool):
            raise _fail(where, "must be true or false")
        return raw
    if not isinstance(raw, str) or len(raw) > schema.DEFAULT_MAX:
        raise _fail(where, f"must be a string of at most {schema.DEFAULT_MAX} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        raise _fail(where, "must not contain control characters")
    if vtype == "enum" and raw not in v["choices"]:
        raise _fail(where, "must be one of the declared choices")
    if vtype == "url":
        try:
            missions._arg_url("variable", v["name"], raw)
        except missions.MissionError as e:
            raise _fail(where, str(e)) from None
    if vtype == "path":
        # The manifest's own path rules: relative, or absolute / `~/`-anchored, and in every case
        # segment-safe — no glob character, no `..`, no `.`, no empty segment.
        try:
            (anchored_path if raw.startswith(("/", "~/")) else relative_path)(raw, where)
        except ManifestError:
            raise _fail(
                where, "must be a path with no glob characters, '..', '.' or empty segments"
            ) from None
    if v["pattern"] is not None and not compile_id_pattern(v["pattern"], where).match(raw):
        raise _fail(where, "does not match the variable's pattern")
    return raw


def _variables(r: _Reader) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for vr in _records(r, "variables", max_items=schema.MAX_VARIABLES):
        name = vr.str("name", pattern=schema.VARIABLE_NAME_RE, max_len=32)
        if name in out:
            raise _fail(vr._f("name"), f"duplicate variable {name!r}")
        kind = vr.str("kind", "text", one_of=frozenset(schema.VARIABLE_KINDS))
        vtype = vr.str("type", "text", one_of=schema.VARIABLE_TYPES)
        if kind == "secret" and vtype != "text":
            raise _fail(vr._f("type"), "a secret variable is text; it takes no other type")
        v = {
            "name": name,
            "kind": kind,
            "type": vtype,
            "label": vr.str("label", name, max_len=schema.LABEL_MAX),
            "help": vr.str("help", "", max_len=schema.HELP_MAX),
            "required": vr.bool("required", False),
            "pattern": None,
            "choices": [],
            "default": None,
        }
        if vr.has("pattern"):
            if vtype not in ("text", "path"):
                raise _fail(vr._f("pattern"), "only a text or path variable takes a pattern")
            compile_id_pattern(vr.raw("pattern"), vr._f("pattern"))
            v["pattern"] = vr.raw("pattern")
        if vtype == "enum":
            choices = list(vr.strs("choices", max_items=schema.MAX_CHOICES))
            if not choices:
                raise _fail(vr._f("choices"), "an enum variable needs at least one choice")
            for i, c in enumerate(choices):
                if len(c) > schema.DEFAULT_MAX or any(ord(ch) < 0x20 for ch in c):
                    raise _fail(f"{vr._f('choices')}[{i}]", "is not a valid choice")
            v["choices"] = choices
        elif vr.has("choices"):
            raise _fail(vr._f("choices"), "only an enum variable takes choices")
        for key in ("default", "example"):
            if not vr.has(key):
                continue
            if kind == "secret":
                # The whole point of #1090's secret kind: no secret value is ever written into a
                # definition. A bundle is a definition someone else authored.
                raise _fail(vr._f(key), f"a secret variable has no {key}")
            v[key] = _variable_default(v, vr.raw(key), vr._f(key))
        vr.done()
        out[name] = v
    return out


def _whole_var_ref(value: Any) -> str | None:
    if isinstance(value, str):
        m = schema.VAR_TOKEN_RE.fullmatch(value)
        if m:
            return m.group(1)
    return None


def _connections(
    r: _Reader, variables: dict[str, dict], targets: dict[str, str]
) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for cr in _records(r, "connections", max_items=schema.MAX_CONNECTIONS):
        name = cr.str("name", pattern=schema.CONNECTION_NAME_RE)
        if name in out:
            raise _fail(cr._f("name"), f"duplicate connection {name!r}")
        kind = cr.str("kind", one_of=schema.CONNECTION_KINDS)
        params: dict[str, str] = {}
        for pname, (required, vtype) in schema.CONNECTION_PARAMS[kind].items():
            if not cr.has(pname):
                if required:
                    raise _fail(cr._f(pname), f"a {kind} connection requires {pname}")
                continue
            raw = cr.raw(pname)
            ref = _whole_var_ref(raw)
            if ref is None:
                raise _fail(
                    cr._f(pname),
                    "must be exactly one {{variable}} reference — a bundle never supplies a "
                    "literal endpoint",
                )
            var = variables.get(ref)
            if var is None:
                raise _fail(cr._f(pname), f"references an undeclared variable {{{{{ref}}}}}")
            if var["kind"] == "secret" or var["type"] != vtype:
                raise _fail(cr._f(pname), f"must reference a {vtype} text variable")
            params[pname] = ref
            targets.setdefault(ref, f"connection {name!r} {pname}")
        credential = None
        if cr.has("credential"):
            credential = cr.str("credential", pattern=schema.VARIABLE_NAME_RE, max_len=32)
            var = variables.get(credential)
            if var is None or var["kind"] != "secret":
                raise _fail(cr._f("credential"), "must name a declared secret variable")
        label = cr.str("label", name, max_len=schema.LABEL_MAX)
        _text_tokens(label, cr._f("label"), variables)
        conn = {
            "name": name,
            "kind": kind,
            "label": label,
            "params": params,
            "credential": credential,
            "verify": cr.str("verify", "none", one_of=schema.CONNECTION_VERIFY[kind]),
        }
        cr.done()
        out[name] = conn
    return out


def _material_path(value: Any, where: str) -> str:
    path = relative_path(value, where)
    if len(path) > schema.PATH_MAX:
        raise _fail(where, f"is longer than {schema.PATH_MAX}")
    for seg in path.split("/"):
        check_segment(seg, where)
    return path


def _materials(r: _Reader) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for mr in _records(r, "materials", max_items=schema.MAX_MATERIALS):
        path = _material_path(mr.raw("path"), mr._f("path"))
        if path in out:
            raise _fail(mr._f("path"), f"duplicate material {path!r}")
        kind = mr.str("kind", schema.MATERIAL_FILE, one_of=schema.MATERIAL_KINDS)
        disposition = mr.str("disposition", "managed", one_of=schema.DISPOSITIONS)
        m: dict[str, Any] = {"path": path, "kind": kind, "disposition": disposition}
        if kind == schema.MATERIAL_ALIAS:
            # §11: an alias is a DECLARATION that deploy turns into a link. Its target is one path
            # component naming another declared file material in the same directory, and the
            # instruction files an engine reads sit at the workspace root — so both are single
            # segments. Never absolute, never `..`, never a file the bundle does not ship.
            if "/" in path:
                raise _fail(mr._f("path"), "an instruction alias sits at the bundle's root")
            target = mr.raw("target")
            if not isinstance(target, str) or "/" in target or target in ("", ".", ".."):
                raise _fail(
                    mr._f("target"),
                    "must be one path component naming a declared material in the same directory",
                )
            check_segment(target, mr._f("target"))
            if disposition != "managed":
                raise _fail(mr._f("disposition"), "an instruction alias is always managed")
            if not (path.endswith(".md") and target.endswith(".md")):
                raise _fail(mr._f("path"), "an instruction alias and its target are .md files")
            m["target"] = target
        else:
            m["template"] = mr.bool("template", False)
        mr.done()
        out[path] = m
    return out


def _cross_check_materials(tree: Tree, materials: dict[str, dict], variables: dict) -> None:
    prefix = f"{schema.MATERIALS_DIR}/"
    on_disk = {p[len(prefix) :] for p in tree.files if p.startswith(prefix)}
    dirs = {p[len(prefix) :] for p in tree.dirs if p.startswith(prefix)}
    for path, m in materials.items():
        where = f"{schema.MANIFEST_NAME}: materials[{path}]"
        if m["kind"] == schema.MATERIAL_ALIAS:
            if path in on_disk or path in dirs:
                raise _fail(where, "an instruction alias is declared, never shipped as a file")
            target = materials.get(m["target"])
            if target is None:
                raise _fail(where, f"alias target {m['target']!r} is not a declared material")
            if target["kind"] != schema.MATERIAL_FILE:
                raise _fail(where, "an instruction alias may not target another alias")
            continue
        if path not in on_disk:
            raise _fail(where, f"is not a file under {prefix}")
    for path in sorted(on_disk):
        m = materials.get(path)
        if m is None or m["kind"] != schema.MATERIAL_FILE:
            raise _fail(f"{prefix}{path}", "is not declared in playbook.toml materials")
        data = tree.files[f"{prefix}{path}"]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            if m["template"]:
                raise _fail(f"{prefix}{path}", "a template material must be UTF-8 text") from None
            continue
        # A rendered material may name only declared, non-secret variables. A verbatim one is never
        # rendered, but a secret's token in it is refused all the same: nothing about a verbatim
        # file should ever look like a place a secret goes.
        _text_tokens(text, f"{prefix}{path}", variables, strict_unknown=m["template"])


def _capabilities(r: _Reader) -> dict[str, bool]:
    t = r.table("capabilities", required=False)
    if t is None:
        return {}
    out: dict[str, bool] = {}
    for key in sorted(str(k) for k in t.t):
        if key not in schema.REQUESTABLE_CAPABILITIES:
            continue  # reported by done() as an unknown field
        out[key] = t.bool(key)
    t.done()
    return out


def _requires(r: _Reader) -> dict:
    t = r.table("requires", required=False)
    if t is None:
        return {"binaries": [], "capabilities": [], "connections": []}
    out = {
        "binaries": list(
            t.strs("binaries", pattern=schema.BINARY_NAME_RE, max_items=schema.MAX_BINARIES)
        ),
        "capabilities": list(t.strs("capabilities", one_of=schema.ENGINE_CAPABILITIES)),
        "connections": list(t.strs("connections", pattern=schema.CONNECTION_NAME_RE)),
    }
    t.done()
    return out


def _rituals(r: _Reader) -> list[dict]:
    out: list[dict] = []
    for rr in _records(r, "rituals", max_items=schema.MAX_RITUALS):
        ritual = {
            "name": rr.str("name", pattern=schema.FILE_ID_RE),
            "runbook": rr.str("runbook", pattern=schema.FILE_ID_RE),
            "schedule": rr.str("schedule", one_of=schema.RITUAL_SCHEDULES),
        }
        rr.done()
        if any(x["name"] == ritual["name"] for x in out):
            raise _fail(rr._f("name"), f"duplicate ritual {ritual['name']!r}")
        out.append(ritual)
    return out


# --- runbooks and templates ----------------------------------------------------------------------


def _dir_files(tree: Tree, dirname: str, suffix: str, max_items: int) -> list[tuple[str, str]]:
    """`(id, relpath)` for every file in a flat bundle directory; anything else refuses."""
    if dirname not in tree.dirs:
        return []
    out: list[tuple[str, str]] = []
    for name in tree.listdir(dirname):
        rel = f"{dirname}/{name}"
        if rel not in tree.files:
            raise _fail(rel, f"{dirname}/ holds only {suffix} files")
        stem = name[: -len(suffix)] if name.endswith(suffix) else ""
        if not schema.FILE_ID_RE.fullmatch(stem):
            raise _fail(rel, f"must be named <id>{suffix} with a lowercase id")
        out.append((stem, rel))
    if len(out) > max_items:
        raise _fail(dirname, f"has more than {max_items} files")
    return out


def _runbook(tree: Tree, rid: str, rel: str, variables: dict, connections: dict) -> dict:
    data = tree.files[rel]
    if len(data) > schema.MAX_RUNBOOK_BYTES:
        raise _fail(rel, f"is larger than {schema.MAX_RUNBOOK_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail(rel, "is not UTF-8") from None
    # Typed frontmatter (#863 §3) in TOML between `+++` fences: the one parser the standard
    # library has, so the frontmatter is exactly as strict as the manifest.
    if not text.startswith("+++\n"):
        raise _fail(rel, "must start with a +++ frontmatter block")
    end = text.find("\n+++\n", 3)
    if end < 0:
        raise _fail(rel, "frontmatter block is not closed with +++")
    try:
        front = tomllib.loads(text[4 : end + 1])
    except tomllib.TOMLDecodeError as e:
        raise _fail(rel, f"frontmatter does not parse: {e}") from None
    except RecursionError:
        raise _fail(rel, "frontmatter is nested too deeply") from None
    body = text[end + 5 :]
    with _in(rel):
        fr = _Reader(front, "")
        if fr.has("id") and fr.str("id", pattern=schema.FILE_ID_RE) != rid:
            raise _fail("id", "must equal the file name")
        fr.raw("id", None)
        out: dict[str, Any] = {
            "id": rid,
            "title": fr.str("title", max_len=schema.NAME_MAX),
            "trigger": fr.str("trigger", one_of=schema.RUNBOOK_TRIGGERS),
            "caps": {},
            "bail": [],
            "requires": {"connections": [], "capabilities": []},
        }
        caps = fr.table("caps", required=False)
        if caps is not None:
            for key, (lo, hi) in schema.RUNBOOK_CAPS.items():
                if caps.has(key):
                    out["caps"][key] = caps.int(key, lo=lo, hi=hi)
            caps.done()
        bail = fr.strs("bail", max_items=schema.MAX_BAIL)
        for i, b in enumerate(bail):
            if len(b) > schema.BAIL_MAX:
                raise _fail(f"bail[{i}]", f"is longer than {schema.BAIL_MAX} characters")
        out["bail"] = list(bail)
        _text_tokens(out["title"], "title", variables)
        for i, b in enumerate(bail):
            _text_tokens(b, f"bail[{i}]", variables)
        req = fr.table("requires", required=False)
        if req is not None:
            conns = list(req.strs("connections", pattern=schema.CONNECTION_NAME_RE))
            for c in conns:
                if c not in connections:
                    raise _fail("requires.connections", f"{c!r} is not a declared connection")
            out["requires"] = {
                "connections": conns,
                "capabilities": list(req.strs("capabilities", one_of=schema.ENGINE_CAPABILITIES)),
            }
            req.done()
        fr.done()
    out["variables"] = _text_tokens(body, rel, variables)
    out["body"] = body
    return out


def _template(tree: Tree, tid: str, rel: str, variables: dict) -> dict:
    """A bundle template, adapted to `templates.validate`'s editable shape (#1190 round 1).

    Variable metadata (type, help, pattern, choices) lives only in `playbook.toml`; the template
    file carries name, description, tags and body. The adapter derives the field records from
    the body's references — name, label, default, `source`, `kind` — and hands the result to
    `templates.validate` unchanged, so the template store's validator is the one that decides.
    Bundled template IMAGES are refused in format 1: `templates.validate` accepts only host-local
    upload paths, and a portable image reference is a later decision.
    """
    doc = _toml(tree, rel, max_bytes=schema.MAX_TOML_BYTES)
    with _in(rel):
        tr = _Reader(doc, "")
        if tr.has("images"):
            raise _fail("images", "bundled template images are not supported in format 1")
        body = tr.raw("body")
        name = tr.raw("name")
        description = tr.raw("description", "")
        tags = tr.raw("tags", [])
        tr.done()
    if not isinstance(body, str):
        raise _fail(f"{rel}: body", "must be a string")
    refs = _text_tokens(body, f"{rel}: body", variables)
    for key, value in (("name", name), ("description", description)):
        if isinstance(value, str):
            _text_tokens(value, f"{rel}: {key}", variables)
    fields = []
    for ref in refs:
        var = variables[ref]
        default = var["default"]
        if isinstance(default, bool):
            default = "true" if default else "false"
        fields.append(
            {
                "name": ref,
                "label": var["label"],
                "default": "" if default is None else str(default),
                "required": var["required"],
                "source": "template",
                "kind": "text",  # a secret was refused above
            }
        )
    editable = {
        "name": name,
        "description": description,
        "tags": tags,
        "body": body,
        "fields": fields,
        "images": [],
    }
    try:
        validated = templates.validate(editable, check_files=False)
    except templates.TemplateError as e:
        raise _fail(rel, str(e)) from None
    return {"id": tid, **validated}


# --- flows ---------------------------------------------------------------------------------------


def _item_key(r: _Reader) -> str:
    key = r.str("key", pattern=schema.ITEM_KEY_RE, max_len=schema.ITEM_KEY_MAX)
    if (
        key.startswith(schema.ITEM_KEY_RESERVED_PREFIXES)
        or key in schema.ITEM_KEY_RESERVED
        or schema.ITEM_KEY_FORBIDDEN_SUBSTRING in key
    ):
        raise _fail(r._f("key"), f"{key!r} is reserved for keys the mission store mints")
    return key


def _actor(r: _Reader) -> dict:
    t = r.table("actor")
    kind = t.str("kind", one_of=schema.ACTOR_KINDS)
    out: dict[str, Any] = {"kind": kind}
    if kind == schema.ACTOR_AGENT:
        out["engine"] = t.str("engine", pattern=schema.ENGINE_REF_RE)
        model = t.str("model", schema.MODEL_DEFAULT, max_len=96)
        if model != schema.MODEL_DEFAULT and not schema.MODEL_REF_RE.fullmatch(model):
            raise _fail(t._f("model"), f"has an invalid shape: {model!r}")
        out["model"] = model
    elif kind == schema.ACTOR_EXTERNAL:
        out["label"] = t.str("label", "external", max_len=schema.LABEL_MAX)
    t.done()
    return out


def _step(sr: _Reader) -> dict:
    step: dict[str, Any] = {
        "id": sr.str("id", pattern=schema.STEP_ID_RE),
        "title": sr.str("title", max_len=schema.ITEM_TITLE_MAX),
        "brief": _prose(sr, "brief", max_len=schema.BRIEF_MAX),
        "actor": _actor(sr),
        "after": list(sr.strs("after", pattern=schema.STEP_ID_RE, max_items=schema.MAX_AFTER)),
        "memory": sr.str("memory", "none", one_of=schema.MEMORY_MODES),
        "outputs": list(sr.strs("outputs", one_of=frozenset(schema.SLOT_TYPES))),
        "checklist": [],
        "rework": None,
        "distinct_from": [],
        "skills": [],
    }
    for ir in _records(sr, "checklist", max_items=schema.MAX_ITEMS_PER_STEP):
        probe = missions.canonical_probe(ir.raw("probe"))
        if not isinstance(probe, str) or probe not in missions.PROBE_KINDS:
            raise _fail(ir._f("probe"), f"unknown probe kind {probe!r}")
        args = ir.raw("probe_args", {})
        if not isinstance(args, dict):
            raise _fail(ir._f("probe_args"), "must be a table")
        item = {
            "key": _item_key(ir),
            "title": ir.str("title", max_len=schema.ITEM_TITLE_MAX),
            "probe": probe,
            "required": ir.bool("required", True),
            "probe_args": dict(args),
            "_where": ir.where,
        }
        ir.done()
        step["checklist"].append(item)
    rw = sr.table("rework", required=False)
    if rw is not None:
        step["rework"] = {
            "to": rw.str("to", pattern=schema.STEP_ID_RE),
            "when": rw.str("when", pattern=schema.ITEM_KEY_RE, max_len=schema.ITEM_KEY_MAX),
            "max_rounds": rw.int(
                "max_rounds", lo=schema.REWORK_ROUNDS_MIN, hi=schema.REWORK_ROUNDS_MAX
            ),
        }
        rw.done()
    for dr in _records(sr, "distinct_from", max_items=schema.MAX_DISTINCT_FROM):
        d = {
            "step": dr.str("step", pattern=schema.STEP_ID_RE),
            "constraint": dr.str("constraint", one_of=schema.DISTINCT_CONSTRAINTS),
        }
        dr.done()
        if d in step["distinct_from"]:
            raise _fail(dr.where, "duplicate distinct_from entry")
        step["distinct_from"].append(d)
    for kr in _records(sr, "skills", max_items=schema.MAX_SKILLS):
        skill = {
            "id": kr.str("id", pattern=schema.SKILL_ID_RE),
            "revision": kr.str("revision", None, pattern=schema.VERSION_RE),
            "required": kr.bool("required", False),
        }
        kr.done()
        step["skills"].append(skill)
    step["_where"] = sr.where
    sr.done()
    return step


def is_note(step: dict) -> bool:
    """A note: `actor: none` with no checklist. It gates nothing and nothing may wait on it."""
    return step["actor"]["kind"] == schema.ACTOR_NONE and not step["checklist"]


def _order(steps: dict[str, dict]) -> list[str]:
    """A topological order over `after`, or the refusal naming a step on a cycle. Rework is NOT an
    edge here: it is the one back-reference, and it is checked against this order's ancestors."""
    indeg = {sid: len(s["after"]) for sid, s in steps.items()}
    children: dict[str, list[str]] = {sid: [] for sid in steps}
    for sid, s in steps.items():
        for parent in s["after"]:
            children[parent].append(sid)
    ready = [sid for sid, n in indeg.items() if n == 0]
    order: list[str] = []
    while ready:
        sid = ready.pop()
        order.append(sid)
        for child in children[sid]:
            indeg[child] -= 1
            if indeg[child] == 0:
                ready.append(child)
    if len(order) != len(steps):
        stuck = sorted(sid for sid in steps if sid not in order)
        raise _fail(steps[stuck[0]]["_where"], f"`after` forms a cycle through {stuck}")
    return order


def _check_arg(
    item: dict,
    name: str,
    value: Any,
    step: dict,
    steps: dict[str, dict],
    ancestors: set[str],
    variables: dict[str, dict],
    targets: dict[str, str],
) -> None:
    kind = item["probe"]
    where = f"{item['_where']}.probe_args.{name}"
    _req, contract = missions.PROBE_ARG_SCHEMA[kind][name]
    arg_type = schema.ARG_TYPE_BY_CONTRACT[contract]
    var = _whole_var_ref(value)
    if var is not None:
        v = variables.get(var)
        if v is None:
            raise _fail(where, f"references an undeclared variable {{{{{var}}}}}")
        if v["kind"] == "secret":
            raise _fail(where, "a secret variable may never be a probe argument")
        if v["type"] not in schema.VAR_TYPES_FOR_ARG[arg_type]:
            raise _fail(where, f"takes a {arg_type} value; {{{{{var}}}}} is a {v['type']} variable")
        if name not in schema.LITERAL_ARGS:
            targets.setdefault(var, f"{where}")
        return
    m = schema.STEP_TOKEN_RE.fullmatch(value) if isinstance(value, str) else None
    if m is not None:
        producer_id, slot = m.group(1), m.group(2)
        producer = steps.get(producer_id)
        if producer is None:
            raise _fail(where, f"references an unknown step {producer_id!r}")
        if producer_id not in ancestors:
            raise _fail(where, f"step {producer_id!r} is not an ancestor of {step['id']!r}")
        if slot not in producer["outputs"]:
            raise _fail(where, f"step {producer_id!r} does not declare the output {slot!r}")
        if schema.SLOT_TYPES[slot] not in schema.ARG_SLOT_TYPES.get(name, frozenset()):
            raise _fail(where, f"{name} does not take a {schema.SLOT_TYPES[slot]} output")
        return
    if isinstance(value, str) and "{{" in value:
        raise _fail(where, "must be exactly one reference, not text around one")
    if name not in schema.LITERAL_ARGS:
        raise _fail(
            where,
            "is target-bearing and takes only a {{variable}} or {{steps.<id>.<slot>}} "
            "reference — a bundle never supplies a literal probe target",
        )
    try:
        contract(kind, name, value)
    except missions.MissionError as e:
        raise _fail(where, str(e)) from None


def _check_graph(
    steps: dict[str, dict], variables: dict[str, dict], targets: dict[str, str]
) -> None:
    for sid, s in steps.items():
        for parent in s["after"]:
            if parent == sid:
                raise _fail(s["_where"], "a step cannot come after itself")
            if parent not in steps:
                raise _fail(s["_where"], f"`after` names an unknown step {parent!r}")
            if is_note(steps[parent]):
                raise _fail(s["_where"], f"{parent!r} is a note and cannot be a prerequisite")
    order = _order(steps)
    ancestors: dict[str, set[str]] = {}
    for sid in order:
        acc: set[str] = set()
        for parent in steps[sid]["after"]:
            acc |= {parent} | ancestors[parent]
        ancestors[sid] = acc
    for sid in order:
        s = steps[sid]
        where = s["_where"]
        kinds = {item["probe"] for item in s["checklist"]}
        producible = set().union(*(schema.OUTPUT_SLOTS.get(k, frozenset()) for k in kinds))
        for slot in s["outputs"]:
            if slot not in producible:
                raise _fail(
                    f"{where}.outputs",
                    f"{slot!r} is not produced by any probe kind in this step's checklist",
                )
        rw = s["rework"]
        if rw is not None:
            target = steps.get(rw["to"])
            if target is None:
                raise _fail(f"{where}.rework.to", f"names an unknown step {rw['to']!r}")
            if is_note(target):
                raise _fail(f"{where}.rework.to", f"{rw['to']!r} is a note")
            if rw["to"] not in ancestors[sid]:
                raise _fail(f"{where}.rework.to", f"{rw['to']!r} is not an ancestor of {sid!r}")
            if rw["when"] not in {item["key"] for item in s["checklist"]}:
                raise _fail(f"{where}.rework.when", "must name an item of this step's checklist")
        for d in s["distinct_from"]:
            if d["step"] == sid:
                raise _fail(f"{where}.distinct_from", "a step cannot be distinct from itself")
            if d["step"] not in steps:
                raise _fail(f"{where}.distinct_from", f"names an unknown step {d['step']!r}")
        _text_tokens(s["brief"], f"{where}.brief", variables)
        _text_tokens(s["title"], f"{where}.title", variables)
        if "label" in s["actor"]:
            _text_tokens(s["actor"]["label"], f"{where}.actor.label", variables)
        for item in s["checklist"]:
            _text_tokens(item["title"], f"{item['_where']}.title", variables)
        for item in s["checklist"]:
            spec = missions.PROBE_ARG_SCHEMA[item["probe"]]
            args = item["probe_args"]
            missing = sorted(n for n, (req, _fn) in spec.items() if req and n not in args)
            if missing:
                raise _fail(
                    f"{item['_where']}.probe_args",
                    f"probe {item['probe']} requires {', '.join(missing)}",
                )
            unknown = sorted(set(args) - set(spec))
            if unknown:
                raise _fail(
                    f"{item['_where']}.probe_args",
                    f"probe {item['probe']} does not take {', '.join(unknown)}",
                )
            for name, value in args.items():
                _check_arg(item, name, value, s, steps, ancestors[sid], variables, targets)


def _flow(tree: Tree, fid: str, rel: str, variables: dict, targets: dict[str, str]) -> dict:
    doc = _format(_toml(tree, rel, max_bytes=schema.MAX_TOML_BYTES), rel)
    with _in(rel):
        fr = _Reader(doc, "")
        fr.raw("format")
        flow: dict[str, Any] = {
            "id": fid,
            "title": fr.str("title", max_len=schema.NAME_MAX),
            "description": fr.str("description", "", max_len=schema.SUMMARY_MAX),
        }
        records = _records(fr, "steps", max_items=schema.MAX_STEPS)
        if not records:
            raise _fail("steps", "a flow needs at least one step")
        steps: dict[str, dict] = {}
        keys: set[str] = set()
        for sr in records:
            s = _step(sr)
            if s["id"] in steps:
                raise _fail(sr._f("id"), f"duplicate step id {s['id']!r}")
            for item in s["checklist"]:
                if item["key"] in keys:
                    raise _fail(f"{item['_where']}.key", f"duplicate item key {item['key']!r}")
                keys.add(item["key"])
            steps[s["id"]] = s
        fr.done()
        _text_tokens(flow["title"], "title", variables)
        _text_tokens(flow["description"], "description", variables)
        _check_graph(steps, variables, targets)
    for s in steps.values():
        s.pop("_where")
        for item in s["checklist"]:
            item.pop("_where")
    flow["steps"] = list(steps.values())
    return flow


def _check_target_variables(variables: dict[str, dict], targets: dict[str, str]) -> None:
    """A variable that chooses a target is TYPED BY THE OPERATOR, never supplied by the bundle.

    A `default` (or an enum's `choices`) on such a variable would let the bundle pick the
    repository, host or path a probe or connection addresses — the literal target the closed
    value space refuses, one indirection away. `example` stays: it is shown, never used. P2's
    per-target confirmation in REVIEW (#1191) may relax this deliberately; this format does not.
    """
    for i, (name, v) in enumerate(variables.items()):
        used = targets.get(name)
        if used is None:
            continue
        for key, present in (
            ("default", v["default"] is not None),
            ("choices", bool(v["choices"])),
        ):
            if present:
                raise _fail(
                    f"{schema.MANIFEST_NAME}: variables[{i}].{key}",
                    f"{{{{{name}}}}} chooses a target ({used}), so its value must be typed by "
                    f"the operator — a target variable takes no {key} (use example to show one)",
                )


# --- the bundle ----------------------------------------------------------------------------------


def _check_root(tree: Tree) -> None:
    for name in tree.listdir(""):
        if name not in schema.ROOT_ENTRIES:
            raise _fail(name, "is not part of the bundle format")
        is_file = name in tree.files
        wants_file = name in (schema.MANIFEST_NAME, schema.README_NAME)
        if is_file != wants_file:
            raise _fail(name, "must be a file" if wants_file else "must be a directory")


def validate_tree(tree: Tree) -> dict:
    """The normalized playbook for a bundle snapshot, or `PlaybookFormatError`."""
    _check_root(tree)
    doc = _format(
        _toml(tree, schema.MANIFEST_NAME, max_bytes=schema.MAX_TOML_BYTES), schema.MANIFEST_NAME
    )
    with _in(schema.MANIFEST_NAME):
        r = _Reader(doc, "")
        r.raw("format")
        identity = _identity(r)
        variables = _variables(r)
        # Every variable that ends up choosing a TARGET — a connection parameter or a target-bearing
        # probe argument — mapped to the first place it does so.
        targets: dict[str, str] = {}
        connections = _connections(r, variables, targets)
        materials = _materials(r)
        capabilities = _capabilities(r)
        rituals = _rituals(r)
        requires = _requires(r)
        verify = list(r.strs("verify", one_of=schema.VERIFY_CHECKS))
        ft = r.table("flows", required=False)
        default_flow = None
        if ft is not None:
            default_flow = ft.str("default", None, pattern=schema.FILE_ID_RE)
            ft.done()
        r.done()
        for c in requires["connections"]:
            if c not in connections:
                raise _fail("requires.connections", f"{c!r} is not a declared connection")
        _text_tokens(identity["name"], "identity.name", variables)
        _text_tokens(identity["publisher"], "identity.publisher", variables)
        _text_tokens(identity["summary"], "identity.summary", variables)
        for i, v in enumerate(variables.values()):
            _text_tokens(v["label"], f"variables[{i}].label", variables)
            _text_tokens(v["help"], f"variables[{i}].help", variables)

    _cross_check_materials(tree, materials, variables)

    readme = ""
    if schema.README_NAME in tree.files:
        data = tree.files[schema.README_NAME]
        if len(data) > schema.MAX_README_BYTES:
            raise _fail(schema.README_NAME, f"is larger than {schema.MAX_README_BYTES} bytes")
        try:
            readme = data.decode("utf-8")
        except UnicodeDecodeError:
            raise _fail(schema.README_NAME, "is not UTF-8") from None
        _text_tokens(readme, schema.README_NAME, variables, strict_unknown=False)

    runbooks = {
        rid: _runbook(tree, rid, rel, variables, connections)
        for rid, rel in _dir_files(tree, schema.RUNBOOKS_DIR, ".md", schema.MAX_RUNBOOKS)
    }
    for i, ritual in enumerate(rituals):
        if ritual["runbook"] not in runbooks:
            raise _fail(
                f"{schema.MANIFEST_NAME}: rituals[{i}].runbook",
                f"{ritual['runbook']!r} is not a runbook in runbooks/",
            )
    bundle_templates = {
        tid: _template(tree, tid, rel, variables)
        for tid, rel in _dir_files(tree, schema.TEMPLATES_DIR, ".toml", schema.MAX_TEMPLATES)
    }
    flows = {
        fid: _flow(tree, fid, rel, variables, targets)
        for fid, rel in _dir_files(tree, schema.FLOWS_DIR, ".toml", schema.MAX_FLOWS)
    }
    _check_target_variables(variables, targets)
    if default_flow is not None and default_flow not in flows:
        raise _fail(
            f"{schema.MANIFEST_NAME}: flows.default", f"{default_flow!r} is not a flow in flows/"
        )
    return {
        "format": schema.FORMAT,
        "identity": identity,
        "variables": list(variables.values()),
        "connections": list(connections.values()),
        "materials": list(materials.values()),
        "capabilities": capabilities,
        "rituals": rituals,
        "requires": requires,
        "verify": verify,
        "default_flow": default_flow,
        "flows": flows,
        "runbooks": runbooks,
        "templates": bundle_templates,
        "readme": readme,
    }
