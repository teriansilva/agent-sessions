"""An MCP server from a repository link (#2353): the package the repository declares, installed as
its PUBLISHED release, matched to the repository at one pinned commit.

`resolve(url)` reads the repository with #2145's fetcher (`skill_repo.fetch`, unchanged: https
only, `gitwrite.admit_destination` with a pinned resolve, no ambient credentials, no redirects,
bounded, reads from git objects only) and decides, deterministically:

* **Root links only.** ``https://<host>/<owner>/<repo>`` with an optional ``.git``; a ``/tree/`` or
  ``/blob/`` link is refused in words (a ref holding ``/`` makes its ref/path split ambiguous).
  The commit is the default branch's head at fetch time, pinned in the result.
* **Exactly one manifest at the root.** ``package.json`` → npm, or ``pyproject.toml`` with a
  ``[project]`` table → PyPI. Both → refused; neither → refused. BattleLab never picks one.
* **A name and a STATIC version.** A missing version, or ``dynamic = ["version"]``, is refused:
  BattleLab never guesses one. A manifest is size-capped and parsed as data (`json` /
  `tomllib`), never executed.
* **The exact release must be published** (npm ``name@version``; PyPI ``name==version`` with a
  wheel, P1b's wheels-only rule), read through P1b's egress proxy (`mcp_egress`, registry hosts
  only). Nothing is built from the repository's code.
* **The registry's repository record must name the link's repository.** Both sides are parsed
  by ONE function (`identity`) into ``(host, owner, repo)``: the host is compared exactly
  (``github.com.evil.example`` is not ``github.com``), owner and repo case-insensitively on known
  forges and exactly elsewhere, ``.git`` / a trailing ``/`` / ``git+https`` / npm's
  ``github:owner/repo`` accepted, any further path ignored. Metadata that is missing or does not
  parse is refused. **A URL found in metadata is never fetched or followed**: it is only parsed.

That match is a consistency check against typos and squatted names, never proof the release was
built from that commit; the Review screen says so.

**A root ``go.mod`` (#2427)** is the exception: a Go module is BUILT from the pinned commit
(`mcp_install`'s Go branch; a Go build runs no repository code). Its identity is proven by the
commit, not by names: the Go module proxy is asked for ``<module>@<pinned commit>`` (no direct VCS
fetch) and its answer's ``Origin.Hash`` must equal the pinned commit, so a mirror link (the same
commit, as on Codeberg) is accepted and shown with the module's home, and a repository whose
``go.mod`` names someone else's module never matches. The version built is the proxy's canonical
one for that commit (its tag, or a pseudo-version). The result is P1b's source unchanged
(``{kind: npm|pypi, package, version}``) plus a display-only ``origin {url, commit}``.

`run` is the polled operation (`mcp_jobs`, kind ``resolve``): resolve, then P1b's own review
(`mcp_install.review`: the exact version into a lock, nothing installed), staged under the server
id the resolution reserves. The server holds ``{id, source, origin}`` under the operation's id;
`create` binds it by ``resolution_id`` (`routes/mcp.py`), never from client fields.
"""

from __future__ import annotations

import json
import re
import threading
import tomllib
from collections.abc import Callable
from urllib.parse import quote, urlsplit

from . import mcp_install, mcp_jobs, skill_repo
from .mcp_egress import EgressProxy, origin
from .mcp_registry import RegistryError

#: A root manifest is read only up to this size (the fetch's own blob filter is 128k).
MANIFEST_MAX = 128 * 1024
#: A registry answer larger than this is refused unread.
REGISTRY_BODY_MAX = 4 * 1024 * 1024
REGISTRY_TIMEOUT_S = 20.0
NPM_META = "https://registry.npmjs.org/"
PYPI_META = "https://pypi.org/pypi/"
#: Forges whose owner and repository names are case-insensitive: compared lowercased there only.
KNOWN_HOSTS = frozenset({"github.com", "gitlab.com", "codeberg.org", "bitbucket.org"})
#: The PyPI ``project_urls`` labels that name a release's repository (normalized: lowercase, no
#: spaces, ``-`` or ``_``), plus the legacy ``home_page``.
PYPI_REPO_LABELS = frozenset({"source", "sourcecode", "repository", "homepage"})
_SEGMENT = re.compile(r"\A[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}\Z")
_INSTALL_SCRIPTS = ("preinstall", "install", "postinstall")

DISCLAIMER = "Installs the published release; not built from this commit."
GO_DISCLAIMER = (
    "Built from this commit with the Go toolchain. No install scripts run; every download is "
    "verified against sum.golang.org."
)
#: ``module <path>`` in go.mod (optionally quoted), one per file.
_GO_MODULE_LINE = re.compile(r'^\s*module\s+"?([^\s"]+)"?\s*(?://.*)?$', re.M)


class LinkRefused(RegistryError):
    """A link that can't be installed safely, in words. ``facts`` is what was read before the
    refusal (the repository, its commit, the manifests found), for the refusal's own panel."""

    def __init__(self, message: str, code: str = "refused", facts: dict | None = None):
        super().__init__(message)
        self.code = code
        self.facts = facts


# ---- repository identity: ONE parser for the link and for registry metadata ---------------------


def identity(raw: object) -> tuple[str, str, str] | None:
    """``(host, owner, repo)`` of a repository address, or None when it isn't one.

    Accepts ``https://``, ``git+https://`` and ``git://`` addresses and npm's ``github:owner/repo``
    shorthand. The host is lowercased and kept whole (a non-default port stays on it); a sign-in
    in the address is not a repository address. ``.git`` and a trailing ``/`` are dropped; any
    path past ``owner/repo`` (a monorepo folder, ``/tree/…``) is ignored for identity.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > skill_repo.URL_MAX:
        return None
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in text):
        return None
    if text.lower().startswith("github:"):
        host, path = "github.com", text[len("github:") :].split("#", 1)[0]
    else:
        scheme, sep, rest = text.partition("://")
        scheme = scheme.lower()
        if not sep or scheme not in ("https", "git+https", "git"):
            return None
        try:
            parts = urlsplit("https://" + rest)
            port = parts.port
        except ValueError:
            return None
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return None
        host = (parts.hostname or "").lower()
        if not host:
            return None
        default = 9418 if scheme == "git" else 443
        if port is not None and port != default:
            host = f"{host}:{port}"
        path = parts.path
    segments = path.split("/")
    if segments and segments[0] == "":
        segments = segments[1:]
    if len(segments) < 2:
        return None
    owner, repo = segments[0], segments[1]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not _SEGMENT.fullmatch(owner) or not _SEGMENT.fullmatch(repo) or repo in (".", ".."):
        return None
    if host in KNOWN_HOSTS:
        owner, repo = owner.lower(), repo.lower()
    return host, owner, repo


def said(ident: tuple[str, str, str]) -> str:
    return "/".join(ident)


def root_link(url: object) -> tuple[str, tuple[str, str, str]]:
    """The operator's link as fetched, and its identity. Only a repository ROOT is admitted."""
    if not isinstance(url, str) or not url.strip():
        raise LinkRefused(
            "paste the repository's link, e.g. https://github.com/owner/repo", "bad_url"
        )
    try:
        link = skill_repo.check_url(url.strip())
    except skill_repo.Refused as e:
        raise LinkRefused(_words(e), e.code) from None
    _repo, ref, folder = skill_repo.split_web_link(link)
    path = urlsplit(link).path
    if ref is not None or folder or re.search(r"/(?:-/)?(?:tree|blob|src)/", path):
        raise LinkRefused(
            "link the repository itself, not a folder or file in it (no /tree/ or /blob/ links): "
            "https://<host>/<owner>/<repo>",
            "not_root",
        )
    ident = identity(link)
    segments = [s for s in path.rstrip("/").split("/") if s]
    if ident is None or len(segments) != 2:
        raise LinkRefused(
            "that is not a repository's root link: use https://<host>/<owner>/<repo>", "not_root"
        )
    return link.rstrip("/"), ident


def _words(e: Exception) -> str:
    """A fetcher refusal without its ``refused:`` prefix: the panel already says it refused."""
    text = str(e)
    return text[len("refused: ") :] if text.startswith("refused: ") else text


# ---- the manifest at the root -------------------------------------------------------------------


def _text_at(snap: skill_repo.Snapshot, path: str, facts: dict) -> str | None:
    entry = snap.entry(path)
    if entry is None:
        return None
    mode, kind, oid = entry
    if kind != "blob" or mode == "120000":
        raise LinkRefused(f"{path} at the repository root is not a regular file", "manifest", facts)
    size = snap.sizes([oid]).get(oid)
    if size is None or size > MANIFEST_MAX:
        kb = MANIFEST_MAX // 1024
        raise LinkRefused(f"{path} is larger than {kb} KB, so it was not read", "manifest", facts)
    try:
        return snap.blob(oid).decode("utf-8")
    except UnicodeDecodeError:
        raise LinkRefused(f"{path} is not UTF-8 text", "manifest", facts) from None


def _found(manifest: str, kind: str, name: object, version: object) -> dict:
    return {
        "manifest": manifest,
        "kind": kind,
        "package": name if isinstance(name, str) else "",
        "version": version if isinstance(version, str) else "",
    }


def _npm_manifest(text: str, facts: dict) -> dict:
    try:
        doc = json.loads(text)
    except ValueError:
        raise LinkRefused("package.json is not valid JSON", "manifest", facts) from None
    if not isinstance(doc, dict):
        raise LinkRefused("package.json is not a package manifest", "manifest", facts)
    return _found("package.json", "npm", doc.get("name"), doc.get("version"))


def _pypi_manifest(text: str, facts: dict) -> dict | None:
    """The ``[project]`` table's package, or None when pyproject.toml declares no project (a tool
    configuration only: not a Python package declaration)."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise LinkRefused("pyproject.toml is not valid TOML", "manifest", facts) from None
    project = doc.get("project")
    if not isinstance(project, dict):
        return None
    found = _found("pyproject.toml", "pypi", project.get("name"), project.get("version"))
    dynamic = project.get("dynamic")
    if isinstance(dynamic, list) and "version" in dynamic:
        found["version"] = ""
        found["dynamic"] = True
    return found


def _go_manifest(text: str, facts: dict) -> dict:
    m = _GO_MODULE_LINE.search(text)
    return _found("go.mod", "go", m.group(1) if m else None, None)


def declared(snap: skill_repo.Snapshot, facts: dict) -> dict:
    """The ONE package the repository root declares, as P1b's source (a Go module's version is
    the proxy's, found by `resolve`). Raises `LinkRefused`."""
    found = []
    npm_text = _text_at(snap, "package.json", facts)
    if npm_text is not None:
        found.append(_npm_manifest(npm_text, facts))
    py_text = _text_at(snap, "pyproject.toml", facts)
    if py_text is not None:
        py = _pypi_manifest(py_text, facts)
        if py is not None:
            found.append(py)
    go_text = _text_at(snap, "go.mod", facts)
    if go_text is not None:
        found.append(_go_manifest(go_text, facts))
    facts["found"] = found
    if not found:
        raise LinkRefused(
            "this repository declares no package at its root: there is no package.json, no "
            "pyproject.toml with a [project] table, and no go.mod",
            "no_manifest",
            facts,
        )
    if len(found) > 1:
        kinds = {f["kind"] for f in found}
        what = (
            "both an npm and a Python package"
            if kinds == {"npm", "pypi"}
            else "more than one kind of package ("
            + " and ".join(f["manifest"] for f in found)
            + ")"
        )
        raise LinkRefused(
            f"this repository declares {what}. BattleLab never picks one for you, so nothing "
            "was registered",
            "both_manifests",
            facts,
        )
    (one,) = found
    if not one["package"]:
        raise LinkRefused(f"{one['manifest']} names no package", "no_name", facts)
    if one["kind"] == "go":
        return {"kind": "go", "package": one["package"]}  # its version is the proxy's (resolve)
    if not one["version"]:
        why = " (its version is dynamic)" if one.get("dynamic") else ""
        raise LinkRefused(
            f"this repository doesn't state a version in {one['manifest']}{why}; BattleLab "
            "never guesses one",
            "no_version",
            facts,
        )
    try:
        source = mcp_install.validate_source(
            {"kind": one["kind"], "package": one["package"], "version": one["version"]}
        )
    except RegistryError as e:
        raise LinkRefused(f"{one['manifest']}: {e}", "manifest", facts) from None
    return source


# ---- the published release ----------------------------------------------------------------------


def _registry_get(kind: str, url: str) -> dict | None:
    """One registry read through P1b's egress proxy (registry hosts only): the JSON object, or
    None when the registry has no such release (404). No redirect is followed, no ambient proxy
    or credential is used, TLS is verified, the body is bounded."""
    import httpx

    allowed = mcp_install._allowed(kind)
    if origin(url) not in allowed:  # never: the URL is built here from validated parts
        raise LinkRefused("that registry address is not allowed", "registry")
    registry = "npm" if kind == "npm" else "PyPI"
    body = bytearray()
    with EgressProxy(allowed) as proxy:
        try:
            with httpx.Client(
                proxy=proxy.url,
                trust_env=False,
                follow_redirects=False,
                timeout=REGISTRY_TIMEOUT_S,
                headers={"Accept": "application/json"},
            ) as client:
                with client.stream("GET", url) as reply:
                    if reply.status_code == 404:
                        return None
                    if reply.status_code != 200:
                        raise LinkRefused(
                            f"the {registry} registry answered {reply.status_code}; try again "
                            "later",
                            "registry",
                        )
                    for chunk in reply.iter_bytes():
                        body += chunk
                        if len(body) > REGISTRY_BODY_MAX:
                            raise LinkRefused(
                                f"the {registry} registry's answer is too large to read",
                                "registry",
                            )
        except httpx.HTTPError:
            raise LinkRefused(
                f"the {registry} registry could not be reached; try again later", "registry"
            ) from None
    try:
        doc = json.loads(bytes(body))
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise LinkRefused(f"the {registry} registry's answer could not be read", "registry")
    return doc


def _unpublished(source: dict, facts: dict) -> LinkRefused:
    registry = "npm" if source["kind"] == "npm" else "PyPI"
    return LinkRefused(
        f"{registry} has no published release {source['package']} {source['version']}. BattleLab "
        "installs only a published release and never builds one from the repository's code, so "
        "nothing was registered",
        "unpublished",
        facts,
    )


def _norm_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _text(value: object, cap: int = 120) -> str:
    return value.strip()[:cap] if isinstance(value, str) else ""


def _npm_release(source: dict, facts: dict) -> tuple[list[object], dict]:
    name, version = source["package"], source["version"]
    doc = _registry_get("npm", f"{NPM_META}{quote(name, safe='@')}/{quote(version, safe='')}")
    if doc is None:
        raise _unpublished(source, facts)
    if doc.get("name") != name or doc.get("version") != version:
        raise LinkRefused("the npm registry answered for another package", "registry", facts)
    repo = doc.get("repository")
    if isinstance(repo, dict) and "url" in repo:
        repo = repo["url"]  # ``{type, url, directory}``; an object without a url is malformed
    candidates = [repo] if repo is not None else []
    scripts = doc.get("scripts") if isinstance(doc.get("scripts"), dict) else {}
    license_ = doc.get("license")
    if isinstance(license_, dict):
        license_ = license_.get("type")
    deps = doc.get("dependencies") if isinstance(doc.get("dependencies"), dict) else {}
    dist = doc.get("dist") if isinstance(doc.get("dist"), dict) else {}
    return candidates, {
        "registry": "registry.npmjs.org",
        "record_field": "repository",
        "license": _text(license_, 80),
        "direct_dependencies": len(deps),
        "install_scripts": [s for s in _INSTALL_SCRIPTS if s in scripts],
        "integrity": _text(dist.get("integrity"), 200),
    }


def _pypi_release(source: dict, facts: dict) -> tuple[list[object], dict]:
    name, version = source["package"], source["version"]
    doc = _registry_get("pypi", f"{PYPI_META}{quote(name, safe='')}/{quote(version, safe='')}/json")
    if doc is None:
        raise _unpublished(source, facts)
    info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
    if _norm_py(str(info.get("name") or "")) != _norm_py(name) or info.get("version") != version:
        raise LinkRefused("PyPI answered for another package", "registry", facts)
    files = doc.get("urls") if isinstance(doc.get("urls"), list) else []
    wheels = [
        f
        for f in files
        if isinstance(f, dict) and f.get("packagetype") == "bdist_wheel" and not f.get("yanked")
    ]
    if not wheels:
        raise LinkRefused(
            f"PyPI has {name} {version} only as source, with no wheel. BattleLab installs wheels "
            "only, since a source package runs its own build",
            "no_wheel",
            facts,
        )
    urls = info.get("project_urls") if isinstance(info.get("project_urls"), dict) else {}
    candidates: list[object] = [
        v for k, v in urls.items() if re.sub(r"[\s_-]+", "", str(k)).lower() in PYPI_REPO_LABELS
    ]
    if info.get("home_page"):
        candidates.append(info["home_page"])
    requires = info.get("requires_dist") if isinstance(info.get("requires_dist"), list) else []
    return candidates, {
        "registry": "pypi.org",
        "record_field": "project_urls",
        "license": _text(info.get("license_expression") or info.get("license"), 80),
        "direct_dependencies": len(requires),
        "install_scripts": [],
        "wheels": len(wheels),
    }


def _match(link_id: tuple[str, str, str], candidates: list[object], facts: dict) -> str:
    """The registry record must name the link's repository; refused in words otherwise. Only the
    parsed identity is ever shown or compared: a metadata URL is never fetched."""
    registry = facts["release"]["registry"]
    field = facts["release"]["record_field"]
    if not candidates:
        raise LinkRefused(
            f"the release on {registry} names no source repository (its {field} is missing), so "
            "it can't be matched to this link",
            "no_metadata",
            facts,
        )
    parsed = [identity(c) for c in candidates]
    if link_id in parsed:
        return said(link_id)
    named = [p for p in parsed if p is not None]
    if not named:
        raise LinkRefused(
            f"the release's repository record on {registry} ({field}) isn't a repository "
            "address BattleLab can compare, so it can't be matched to this link",
            "bad_metadata",
            facts,
        )
    raise LinkRefused(
        f"the release on {registry} names a different repository ({said(named[0])}), not "
        f"{said(link_id)}. It may be another package with the same name",
        "mismatch",
        facts,
    )


# ---- the resolution -----------------------------------------------------------------------------


def _go_release(module: str, commit: str, link_id: tuple[str, str, str], facts: dict) -> dict:
    """The Go module at exactly the pinned commit, from the Go module proxy (#2427): its canonical
    version and home. Refused unless the proxy's ``Origin.Hash`` is the pinned commit."""
    import shutil
    import tempfile
    from pathlib import Path

    from . import mcp_gotool

    if not mcp_install._GO_MODULE.fullmatch(module):
        raise LinkRefused(f"go.mod's module {module!r} is not a module path BattleLab builds",
                          "manifest", facts)  # fmt: skip
    mcp_install.root().mkdir(parents=True, exist_ok=True, mode=0o700)
    cwd = Path(tempfile.mkdtemp(prefix="golist-", dir=mcp_install.root()))
    try:
        with mcp_gotool.job():
            try:
                go = mcp_gotool.ensure(None)
            except RegistryError as e:
                raise LinkRefused(
                    f"the Go toolchain could not be had: {e}", "toolchain", facts
                ) from None
            with mcp_gotool.building():
                doc = mcp_install._go_json(
                    go,
                    ["list", "-m", "-json", f"{module}@{commit}"],
                    cwd,
                    "asking the Go module proxy",
                    mcp_install.GO_STEP_TIMEOUT_S,
                )
    except LinkRefused:
        raise
    except mcp_gotool.ToolchainError as e:  # e.g. another Go build kept the build slot
        raise LinkRefused(str(e), "toolchain", facts) from None
    except RegistryError as e:
        raise LinkRefused(
            f"{e}. The Go module proxy has no {module} at this commit ({commit[:7]}): it isn't in "
            "the module's own repository, or the proxy doesn't serve it, so nothing was registered",
            "unpublished",
            facts,
        ) from None
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    found = doc.get("Origin") if isinstance(doc.get("Origin"), dict) else {}
    if doc.get("Path") != module or found.get("Hash") != commit:
        raise LinkRefused(
            f"the Go module proxy's {module} is not at this repository's commit ({commit[:7]}), "
            "so this repository is not that module's code. Nothing was registered",
            "provenance",
            facts,
        )
    try:
        source = mcp_install.validate_source(
            {"kind": "go", "package": module, "version": doc.get("Version")}
        )
    except RegistryError as e:
        raise LinkRefused(f"the Go module proxy's answer: {e}", "registry", facts) from None
    home_url = found.get("URL") if isinstance(found.get("URL"), str) else ""
    home_id = identity(home_url)
    facts["release"] = {
        "module": module,
        "version": source["version"],
        "home": said(home_id) if home_id else _text(home_url),
        "mirror": bool(home_id) and home_id != link_id,
        "same_commit": True,
        "go": _text(doc.get("GoVersion") or "", 20),
        "time": _text(doc.get("Time") or "", 40),
        "ref": _text(found.get("Ref") or "", 120),
    }
    return source


def _fetch(url: str, cancel: threading.Event | None) -> skill_repo.Snapshot:
    return skill_repo.fetch(url, cancel=cancel)


def _no_progress(step: str, **facts: object) -> None:
    return None


def resolve(
    url: object,
    *,
    cancel: threading.Event | None = None,
    progress: Callable[..., None] = _no_progress,
) -> dict:
    """``{source, origin, facts}`` for a repository link. Blocking (a fetch and a registry read).
    Raises `LinkRefused` in words; whatever happens, the fetched folder is removed."""
    link, link_id = root_link(url)
    origin_url = skill_repo.normalize(link)
    facts: dict = {"repository": said(link_id), "url": origin_url, "commit": ""}
    progress("commit")
    try:
        snap = _fetch(link, cancel)
    except skill_repo.Cancelled:
        raise LinkRefused("stopped: the link was not read", "stopped", facts) from None
    except skill_repo.Refused as e:
        raise LinkRefused(_words(e), e.code, facts) from None
    try:
        facts["commit"] = snap.commit
        progress("manifest", commit=snap.commit)
        source = declared(snap, facts)
    except skill_repo.Refused as e:
        raise LinkRefused(_words(e), e.code, facts) from None
    finally:
        skill_repo.remove(snap.work)
    facts["declared"] = facts["found"][0]
    progress("release", **facts["declared"])
    if cancel is not None and cancel.is_set():
        raise LinkRefused("stopped: the link was not read", "stopped", facts)
    if source["kind"] == "go":
        # The same steps the page shows: `release` = the module at this commit from the proxy
        # (its first use also fetches the toolchain), `repository` = the commit match.
        try:
            source = _go_release(source["package"], snap.commit, link_id, facts)
        except mcp_jobs.Stopped:
            raise LinkRefused("stopped: the link was not read", "stopped", facts) from None
        progress("repository", version=source["version"])
        origin_rec = {"url": origin_url, "commit": snap.commit}
        return {"source": source, "origin": origin_rec, "facts": facts}
    reader = _npm_release if source["kind"] == "npm" else _pypi_release
    candidates, release = reader(source, facts)
    facts["release"] = release
    progress("repository")
    facts["release"]["names"] = _match(link_id, candidates, facts)
    return {"source": source, "origin": {"url": origin_url, "commit": snap.commit}, "facts": facts}


def run(op) -> dict:
    """The ``resolve`` operation (`mcp_jobs`): resolve the link, then P1b's review of exactly that
    source (a lock, nothing installed), staged under the server id the resolution reserves. Sets
    the server-held binding ``op.bound``; returns what the client sees."""
    resolved = resolve(op.link, cancel=op.cancel, progress=op.report)
    if op.cancel.is_set():
        raise LinkRefused("stopped: the link was not read", "stopped", resolved["facts"])
    disclaimer = GO_DISCLAIMER if resolved["source"]["kind"] == "go" else DISCLAIMER
    current = op.current or {}
    if op.update_of and current.get("source") == resolved["source"] and current.get("installed"):
        # Update from source (#2427): the link's head is what is installed. Nothing to apply.
        return {
            "resolution_id": op.id,
            "up_to_date": True,
            "source": resolved["source"],
            "origin": resolved["origin"],
            "previous": {"source": current.get("source"), "origin": current.get("origin")},
            "facts": resolved["facts"],
            "disclaimer": disclaimer,
        }
    op.report("review")
    try:
        review = mcp_install.review({"id": op.reserved, "source": resolved["source"]})
    except mcp_jobs.Stopped:  # the review removed its own staging on the way out
        raise LinkRefused("stopped: the link was not read", "stopped", resolved["facts"]) from None
    except RegistryError as e:
        said = (
            f"BattleLab can't build this Go module: {e}"
            if resolved["source"]["kind"] == "go"
            else f"the release matches, but its review failed: {e}"
        )
        raise LinkRefused(said, "review", resolved["facts"]) from None
    if op.cancel.is_set():  # stopped just after the review: nothing of it is kept
        import shutil

        shutil.rmtree(mcp_install._staging(op.reserved, review["review_id"]), ignore_errors=True)
        raise LinkRefused("stopped: the link was not read", "stopped", resolved["facts"])
    op.bound = {"id": op.reserved, "source": resolved["source"], "origin": resolved["origin"]}
    if op.update_of:
        # What the server was when its link was read: applying requires it unchanged (#2427).
        op.bound["previous"] = {"source": current.get("source"), "origin": current.get("origin")}
    out = {
        "resolution_id": op.id,
        "source": resolved["source"],
        "origin": resolved["origin"],
        "facts": resolved["facts"],
        "review": review,
        "disclaimer": disclaimer,
    }
    if op.update_of:
        out["up_to_date"] = False
        out["previous"] = {"source": current.get("source"), "origin": current.get("origin")}
    return out


__all__ = [
    "DISCLAIMER",
    "GO_DISCLAIMER",
    "LinkRefused",
    "declared",
    "identity",
    "resolve",
    "root_link",
    "run",
]
