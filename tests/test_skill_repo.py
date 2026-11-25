"""Reading a skill out of a repository link (#2145): `skill_repo`, with real git.

Pinned here, each against real git (2.43 on CI) and either a bare repository on disk or a real TLS
server on loopback:

* admission refuses this machine, a credential in the URL, and every non-https scheme;
* the link must be a COMPLETE URL the Commander typed: a prefix, a substring or a link that only
  appears in tool content is refused;
* a redirect is refused in words, and nothing past it is fetched;
* no ambient credential is ever offered: a credential helper, `~/.git-credentials` and `~/.netrc`
  in the process HOME are never consulted (the server records every Authorization header);
* budgets: unselected oversized content under the ceiling still lists and imports the small skill;
  a server that IGNORES the filter still imports the small skill and refuses an oversized file at
  read time; a repository over the aggregate ceiling is killed mid-fetch by the watchdog, refused
  in words, and its folder and processes are gone; the same for the per-file `RLIMIT_FSIZE`, the
  wall clock, and a cancellation;
* a hostile tree (symlinks, binaries, oversize files, more than 64 files, deep paths, `.git` and
  `..` entries) is refused with every reason;
* a snapshot is pinned to its commit: a later push changes nothing it reads.

The only test seams are mechanics: a bare repository on disk stands in for an https remote
(`local`), and the TLS server's own CA and loopback address are pinned through the SAME pin
arguments `gitwrite.tls_pin_for` builds (`tls`).
"""

from __future__ import annotations

import datetime
import http.server
import os
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agent_sessions import fsize_exec, gitwrite, skill_repo

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.test",
}


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(
        ["git", *args], cwd=cwd, env={**env, **GIT_ENV}, input=stdin, capture_output=True,
        check=True,
    )  # fmt: skip
    return out.stdout.decode()


def make_repo(root: Path, files: dict[str, bytes | str], *, allow_filter: bool = True) -> Path:
    """A bare 'server' repository holding exactly ``files`` (a value ``("link", target)`` is a
    symlink), with `uploadpack.allowFilter` as asked."""
    work = root / "work"
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "core.compression", "0")
    for path, data in files.items():
        p = work / path
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, tuple):
            os.symlink(data[1], p)
        else:
            p.write_bytes(data.encode() if isinstance(data, str) else data)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    srv = root / "srv.git"
    _git(root, "clone", "-q", "--bare", str(work), str(srv))
    _git(srv, "config", "uploadpack.allowFilter", "true" if allow_filter else "false")
    _git(srv, "config", "core.compression", "0")
    _git(srv, "config", "pack.compression", "0")
    # No delta search either: on a loaded CI runner, upload-pack trying deltas across many
    # incompressible blobs took longer than the fetch clock before a byte arrived.
    _git(srv, "config", "pack.window", "0")
    return srv


SKILL = "---\nname: pdf-forms\ndescription: Fill and flatten PDF forms.\n---\n\n# PDF forms\n"


@pytest.fixture
def local(monkeypatch):
    """MECHANICS ONLY: a bare repository on disk is the only remote a hermetic test can build, so
    the https-only URL check and the admission are replaced by the `file` transport here."""
    monkeypatch.setattr(skill_repo, "check_url", lambda u: u)
    monkeypatch.setattr(
        skill_repo, "_admit", lambda u: gitwrite.Admission([], allow_protocol="file")
    )


@pytest.fixture
def pids(monkeypatch):
    """Every process group the fetch starts (its group id is the child's pid)."""
    seen: list[int] = []
    real = subprocess.Popen

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen.append(self.pid)

    monkeypatch.setattr(skill_repo.subprocess, "Popen", Recording)
    return seen


def _live_members(pgid: int) -> list[int]:
    """Processes of group ``pgid`` that can still RUN. A zombie is dead: a SIGKILLed helper git
    started is reparented and waits for init to reap it, and ``killpg(pgid, 0)`` still finds it
    (under load that window is long enough to fail a check made right after the fetch)."""
    live = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        fields = stat[stat.rindex(")") + 2 :].split()  # after "pid (comm) ": state, ppid, pgrp
        if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
            live.append(int(entry))
    return live


def _group_gone(pgid: int, wait_s: float = 5.0) -> bool:
    """No process of the group can run any more. Polled briefly: SIGKILL is delivered
    asynchronously, so a member may still be leaving the kernel when the fetch returns."""
    deadline = time.monotonic() + wait_s
    while True:
        if not _live_members(pgid):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


def _left() -> list[str]:
    base = skill_repo.root()
    return sorted(p.name for p in base.iterdir()) if base.exists() else []


# ---- the link -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/acme/skills.git",
        "https://localhost/acme/skills.git",
        "https://user:token@github.com/acme/skills.git",
        "https://token@github.com/acme/skills.git",
        "file:///srv/skills.git",
        "ssh://git@github.com/acme/skills.git",
        "git@github.com:acme/skills.git",
        "git://github.com/acme/skills.git",
        "http://github.com/acme/skills.git",
        "https://github.com/acme/skills.git?token=abc",
    ],
)
def test_admission_refuses_this_machine_credentials_and_every_non_https_scheme(url):
    with pytest.raises(skill_repo.Refused) as e:
        skill_repo.fetch(url)
    assert e.value.code in {"bad_url", "local"}, e.value
    assert _left() == []


QUESTION = "Import the skill from https://GitHub.com/acme/agent-skills.git — the pdf-forms one."


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/agent-skills",
        "https://github.com/acme/agent-skills.git",
        "https://github.com/acme/agent-skills/",
        "HTTPS://GITHUB.COM/acme/agent-skills",
    ],
)
def test_the_link_the_commander_typed_is_accepted_after_normalization(url):
    assert skill_repo.operator_url(url, QUESTION) == url


@pytest.mark.parametrize(
    "url, question",
    [
        # a prefix of what was typed
        ("https://github.com/acme", QUESTION),
        # a substring
        ("https://github.com/acme/agent", QUESTION),
        # longer than what was typed
        ("https://github.com/acme/agent-skills/tree/main/x", QUESTION),
        # only in tool content: the message names no link at all
        ("https://github.com/acme/agent-skills", "Import the pdf-forms skill from that repo"),
        # another host than the one typed
        ("https://evil.test/acme/agent-skills", QUESTION),
        # path case differs (paths are case-sensitive on forges)
        ("https://github.com/ACME/agent-skills", QUESTION),
    ],
)
def test_a_prefix_a_substring_or_an_untyped_link_is_refused(url, question):
    with pytest.raises(skill_repo.Refused) as e:
        skill_repo.operator_url(url, question)
    assert e.value.code == "not_typed"


def test_a_link_in_brackets_or_markdown_is_still_a_complete_token():
    for q in (
        "import <https://github.com/acme/skills>",
        "import [skills](https://github.com/acme/skills).",
        "import `https://github.com/acme/skills`, please",
    ):
        assert skill_repo.operator_url("https://github.com/acme/skills", q)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/acme/skills", ("https://github.com/acme/skills", None, "")),
        (
            "https://github.com/acme/skills/tree/main/skills/pdf-forms",
            ("https://github.com/acme/skills", "main", "skills/pdf-forms"),
        ),
        (
            "https://gitlab.com/g/sub/skills/-/tree/v2/pdf",
            ("https://gitlab.com/g/sub/skills", "v2", "pdf"),
        ),
        (
            "https://git.example.org/acme/skills/src/branch/main/pdf/",
            ("https://git.example.org/acme/skills", "main", "pdf"),
        ),
    ],
)
def test_a_folder_link_on_a_forge_page_names_its_repository_by_its_own_prefix(url, expected):
    assert skill_repo.split_web_link(url) == expected
    assert url.startswith(expected[0])


# ---- a real TLS server on loopback --------------------------------------------------------------


def _cert(tmp: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "forge.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("forge.test")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class Forge:
    """Answers every request with ``mode``: a redirect elsewhere, a sign-in demand, or a hang."""

    def __init__(self, tmp: Path, mode: str):
        self.mode = mode
        self.requests: list[dict] = []
        self.release = threading.Event()
        cert, key = _cert(tmp)
        self.ca = cert
        forge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):  # noqa: N802
                forge.requests.append({"path": self.path, "headers": dict(self.headers)})
                if forge.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://other.test/acme/skills.git/info/refs")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif forge.mode == "auth":
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="forge"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:  # hang until released: the transport helper waits on us
                    forge.release.wait(60)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.url = f"https://forge.test:{self.port}/acme/skills.git"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def tls(tmp_path, monkeypatch):
    """A real TLS forge on loopback. MECHANICS ONLY: admission would (rightly) refuse loopback, so
    the test pins `forge.test` to 127.0.0.1 with the production pin set (`tls_pin_for`: url-specific
    sslVerify, the proxy reset, redirects off) plus `curloptResolve`, and trusts the test CA."""
    made: list[Forge] = []

    def start(mode: str) -> Forge:
        forge = Forge(tmp_path, mode)
        made.append(forge)
        monkeypatch.setattr(
            skill_repo,
            "_admit",
            lambda u: gitwrite.Admission(
                [
                    *gitwrite.tls_pin_for(u),
                    "-c",
                    f"http.curloptResolve=forge.test:{forge.port}:127.0.0.1",
                ],
                allow_protocol="https",
            ),
        )
        real_env = skill_repo._env

        def env(work, **kw):
            return {**real_env(work, **kw), "GIT_SSL_CAINFO": str(forge.ca)}

        monkeypatch.setattr(skill_repo, "_env", env)
        return forge

    yield start
    for f in made:
        f.close()


def test_a_redirect_is_refused_in_words_and_never_followed(tls):
    forge = tls("redirect")
    with pytest.raises(skill_repo.Refused) as e:
        skill_repo.fetch(forge.url)
    assert e.value.code == "redirect", e.value
    assert "redirect" in str(e.value)
    assert len(forge.requests) == 1 and forge.requests[0]["path"].startswith("/acme/skills.git/")
    assert _left() == []


def test_no_ambient_credential_is_ever_sent(tls, tmp_path, monkeypatch):
    """A helper, `~/.git-credentials` and `~/.netrc` all exist in the process HOME (and its XDG
    config); the server demands a sign-in; nothing is offered, and the helper never runs."""
    forge = tls("auth")
    home = tmp_path / "operator-home"
    (home / ".config" / "git").mkdir(parents=True)
    marker = tmp_path / "helper-ran"
    helper = home / "helper.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\necho username=u\necho password=hunter2\n")
    helper.chmod(0o700)
    for cfg in (home / ".gitconfig", home / ".config" / "git" / "config"):
        cfg.write_text(f"[credential]\n\thelper = {helper}\n\thelper = store\n")
    (home / ".git-credentials").write_text(f"https://u:hunter2@forge.test:{forge.port}\n")
    (home / ".netrc").write_text("machine forge.test login u password hunter2\n")
    (home / ".netrc").chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(skill_repo.Refused) as e:
        skill_repo.fetch(forge.url)
    assert e.value.code == "private", e.value
    assert forge.requests, "the server was never reached"
    assert all("Authorization" not in r["headers"] for r in forge.requests), forge.requests
    assert not marker.exists()
    assert _left() == []


def test_a_fetch_past_the_wall_clock_is_killed_with_its_whole_group(tls, monkeypatch, pids):
    forge = tls("hang")
    monkeypatch.setattr(skill_repo, "FETCH_TIMEOUT_S", 1.0)
    began = time.monotonic()
    with pytest.raises(skill_repo.Refused) as e:
        skill_repo.fetch(forge.url)
    assert e.value.code == "timeout" and "took longer" in str(e.value)
    assert time.monotonic() - began < 10
    assert forge.requests, "git never reached the server, so nothing was hanging"
    assert pids and all(_group_gone(p) for p in pids)
    assert _left() == []


def test_a_cancelled_fetch_is_killed_with_its_whole_group(tls, pids):
    forge = tls("hang")
    cancel = threading.Event()
    result: list[BaseException] = []

    def run():
        try:
            skill_repo.fetch(forge.url, cancel=cancel)
        except BaseException as e:  # noqa: BLE001
            result.append(e)

    t = threading.Thread(target=run)
    t.start()
    deadline = time.monotonic() + 15
    while not forge.requests and time.monotonic() < deadline:
        time.sleep(0.05)
    assert forge.requests, "git never reached the server"
    cancel.set()
    t.join(10)
    assert not t.is_alive()
    assert result and isinstance(result[0], skill_repo.Cancelled)
    assert pids and all(_group_gone(p) for p in pids)
    assert _left() == []


# ---- budgets, with real git and a bare repository on disk ---------------------------------------


def test_oversized_unselected_content_under_the_ceiling_still_lists_and_imports(tmp_path, local):
    srv = make_repo(
        tmp_path,
        {
            "skills/pdf-forms/SKILL.md": SKILL,
            "skills/pdf-forms/reference/fields.md": "| Type | Value |\n",
            "assets/huge.bin": os.urandom(5 * 1024 * 1024),
        },
    )
    snap = skill_repo.fetch(f"file://{srv}")
    try:
        # The filter held: the 5 MiB blob was never sent.
        assert snap.peak < 1024 * 1024 and not snap.filter_ignored
        listed = skill_repo.listing(snap)
        assert [s["path"] for s in listed["skills"]] == ["skills/pdf-forms"]
        assert listed["skills"][0]["name"] == "pdf-forms"
        assert listed["skills"][0]["description"] == "Fill and flatten PDF forms."
        read = skill_repo.read_skill(snap, "skills/pdf-forms")
        assert read.reasons == []
        assert read.files == {"SKILL.md": SKILL, "reference/fields.md": "| Type | Value |\n"}
    finally:
        skill_repo.remove(snap.work)


def test_a_server_that_ignores_the_filter_still_imports_and_refuses_oversize_at_read(
    tmp_path, local
):
    big = "x" * (200 * 1024)
    srv = make_repo(
        tmp_path,
        {
            "small/SKILL.md": SKILL,
            "large/SKILL.md": SKILL,
            "large/reference/huge.md": big,
        },
        allow_filter=False,
    )
    snap = skill_repo.fetch(f"file://{srv}")
    try:
        assert snap.filter_ignored, "the fixture server should have ignored the filter"
        assert snap.peak > 200 * 1024  # the oversized blob WAS downloaded
        assert skill_repo.read_skill(snap, "small").files == {"SKILL.md": SKILL}
        refused = skill_repo.read_skill(snap, "large")
        assert refused.files == {}
        assert refused.reasons == ["reference/huge.md is 200.0 KB (limit 128 KB)"]
        assert {r["path"]: r["flag"] for r in refused.rows} == {
            "SKILL.md": "",
            "reference/huge.md": "oversize",
        }
    finally:
        skill_repo.remove(snap.work)


def test_a_filtered_blob_is_missing_never_lazily_fetched(tmp_path, local):
    srv = make_repo(tmp_path, {"s/SKILL.md": SKILL, "s/big.md": "y" * (300 * 1024)})
    snap = skill_repo.fetch(f"file://{srv}")
    try:
        read = skill_repo.read_skill(snap, "s")
        assert read.reasons == ["big.md is over 128 KB (limit 128 KB)"]
        # Nothing was fetched to answer it: the folder is still the filtered clone's size.
        assert skill_repo.tree_bytes(snap.work) < 200 * 1024
    finally:
        skill_repo.remove(snap.work)


def test_a_repository_over_the_ceiling_is_killed_mid_fetch_by_the_watchdog(
    tmp_path, local, monkeypatch, pids, capsys
):
    """A server that IGNORES the filter would send 96 MiB; the watchdog kills git's group past the
    real 32 MiB threshold. `RLIMIT_FSIZE` and the wall clock are lifted here so the watchdog alone
    is under test (each has its own test); on a loaded runner the 60 s clock could fire before the
    first byte. A local disk transfer is the fastest case there is, so it bounds the poll-interval
    overshoot."""
    monkeypatch.setattr(skill_repo, "FSIZE_BYTES", 256 * 1024 * 1024)
    monkeypatch.setattr(skill_repo, "FETCH_TIMEOUT_S", 600.0)
    files = {"s/SKILL.md": SKILL}
    files.update({f"blobs/{i}.bin": os.urandom(4 * 1024 * 1024) for i in range(24)})
    srv = make_repo(tmp_path, files, allow_filter=False)
    peaks: list[int] = []
    real = skill_repo.tree_bytes

    def measured(path):
        n = real(path)
        peaks.append(n)
        return n

    monkeypatch.setattr(skill_repo, "tree_bytes", measured)
    with pytest.raises(skill_repo.Refused) as e:
        skill_repo.fetch(f"file://{srv}")
    assert e.value.code == "too_large" and "larger than 32 MiB" in str(e.value)
    peak = max(peaks)
    with capsys.disabled():
        print(f"\n[#2145 evidence] watchdog: peak temp-dir usage {peak} bytes "
              f"({peak / 2**20:.1f} MiB) against the 32 MiB threshold")  # fmt: skip
    assert skill_repo.MAX_REPO_BYTES < peak < 96 * 1024 * 1024  # killed before it all arrived
    assert pids and all(_group_gone(p) for p in pids)
    assert _left() == []


def test_a_single_file_past_rlimit_fsize_is_killed_and_refused(tmp_path, local, monkeypatch, pids):
    monkeypatch.setattr(skill_repo, "FSIZE_BYTES", 1024 * 1024)
    srv = make_repo(
        tmp_path, {"s/SKILL.md": SKILL, "big.bin": os.urandom(3 * 1024 * 1024)}, allow_filter=False
    )
    with pytest.raises(skill_repo.Refused) as e:
        skill_repo.fetch(f"file://{srv}")
    assert e.value.code == "too_large", e.value
    assert pids and all(_group_gone(p) for p in pids)
    assert _left() == []


def test_the_trampoline_refuses_anything_but_a_bounded_limit():
    with pytest.raises(fsize_exec.Refused):
        fsize_exec.argv("/usr/bin/python3", 0, ["git"])
    with pytest.raises(fsize_exec.Refused):
        fsize_exec.argv("/usr/bin/python3", fsize_exec.MAX + 1, ["git"])
    with pytest.raises(fsize_exec.Refused):
        fsize_exec.parse(["--fsize", "-1", "--", "git"])
    assert fsize_exec.parse(["--fsize", "1024", "--", "git", "x"]) == (1024, ["git", "x"])


# ---- the tree -----------------------------------------------------------------------------------


def test_a_hostile_skill_folder_is_refused_with_every_reason(tmp_path, local):
    files: dict = {
        "evil/SKILL.md": SKILL,
        "evil/link.md": ("link", "../../etc/passwd"),
        "evil/form.pdf": b"%PDF-1.7\x00\x01\x02",
        "evil/a/b/c/d/e/f/g/h/deep.md": "deep\n",
    }
    srv = make_repo(tmp_path, files)
    snap = skill_repo.fetch(f"file://{srv}")
    try:
        read = skill_repo.read_skill(snap, "evil")
        assert read.files == {}
        flags = {r["path"]: r["flag"] for r in read.rows}
        assert flags["link.md"] == "link"
        assert flags["form.pdf"] == "binary"
        assert flags["a/b/c/d/e/f/g/h/deep.md"] == "path"
        assert "link.md is a symbolic link" in read.reasons
        assert "form.pdf is binary" in read.reasons
    finally:
        skill_repo.remove(snap.work)


def test_more_than_64_files_is_refused(tmp_path, local):
    files = {"many/SKILL.md": SKILL, **{f"many/r/{i}.md": f"{i}\n" for i in range(70)}}
    snap = skill_repo.fetch(f"file://{make_repo(tmp_path, files)}")
    try:
        read = skill_repo.read_skill(snap, "many")
        assert read.files == {} and read.reasons[0].startswith("it has 71 files")
    finally:
        skill_repo.remove(snap.work)


def test_dot_git_and_dot_dot_tree_entries_are_refused(tmp_path, local):
    """Trees git itself would never check out, built object by object: still refused by path."""
    srv = make_repo(tmp_path, {"ok/SKILL.md": SKILL})
    blob = _git(srv, "hash-object", "-w", "--stdin", stdin=b"x\n").strip()
    skill_blob = _git(srv, "rev-parse", "HEAD:ok/SKILL.md").strip()
    inner = _git(
        srv,
        "mktree",
        stdin=(
            f"100644 blob {skill_blob}\tSKILL.md\n100644 blob {blob}\t.git\n"
            f"100644 blob {blob}\tx:y\n"
        ).encode(),
    ).strip()
    top = _git(srv, "mktree", stdin=f"040000 tree {inner}\tbad\n".encode()).strip()
    commit = _git(srv, "commit-tree", top, "-m", "hostile").strip()
    _git(srv, "update-ref", "refs/heads/main", commit)
    snap = skill_repo.fetch(f"file://{srv}")
    try:
        read = skill_repo.read_skill(snap, "bad")
        flags = {r["path"]: r["flag"] for r in read.rows}
        assert flags[".git"] == "path" and flags["x:y"] == "path"
        assert read.files == {}
    finally:
        skill_repo.remove(snap.work)


def test_a_path_inspect_did_not_list_is_refused(tmp_path, local):
    snap = skill_repo.fetch(f"file://{make_repo(tmp_path, {'s/SKILL.md': SKILL, 'x/a.md': 'a'})}")
    try:
        for bad in ("x", "s/..", "", "nope"):
            with pytest.raises(skill_repo.Refused):
                skill_repo.read_skill(snap, bad)
    finally:
        skill_repo.remove(snap.work)


def test_an_empty_repository_lists_no_skills_and_its_top_level(tmp_path, local):
    srv = make_repo(tmp_path, {"README.md": "hi\n", "journal/a.md": "a\n"})
    snap = skill_repo.fetch(f"file://{srv}")
    try:
        listed = skill_repo.listing(snap)
        assert listed["skills"] == [] and listed["top_level"] == ["README.md", "journal/"]
    finally:
        skill_repo.remove(snap.work)


def test_a_pinned_commit_ignores_a_later_push(tmp_path, local):
    srv = make_repo(tmp_path, {"s/SKILL.md": SKILL})
    snap = skill_repo.fetch(f"file://{srv}")
    try:
        work = tmp_path / "work"
        (work / "s" / "SKILL.md").write_text(SKILL + "\nInjected later.\n")
        _git(work, "commit", "-qam", "later")
        _git(work, "push", "-q", str(srv), "main")
        assert skill_repo.read_skill(snap, "s").files == {"SKILL.md": SKILL}
        again = skill_repo.fetch(f"file://{srv}")
        try:
            assert again.commit != snap.commit
            assert "Injected later." in skill_repo.read_skill(again, "s").files["SKILL.md"]
        finally:
            skill_repo.remove(again.work)
    finally:
        skill_repo.remove(snap.work)


def test_a_turn_removes_its_repositories_when_it_ends(tmp_path, local):
    srv = make_repo(tmp_path, {"s/SKILL.md": SKILL})
    repos = skill_repo.TurnRepos()
    ref = repos.add(skill_repo.fetch(f"file://{srv}"))
    assert ref == "skillsrc:1" and repos.get(ref) is not None and _left()
    repos.close()
    assert repos.get(ref) is None and _left() == []
    late = skill_repo.fetch(f"file://{srv}")
    with pytest.raises(skill_repo.Refused):
        repos.add(late)  # a fetch that finished after the turn ended is not kept
    assert _left() == []


def test_a_stop_cancels_the_turns_fetch_and_reaps_it(tls, pids):
    """HQ's Stop cancels a running read (`ask_orchestrator._stoppable`): the cancellation reaches
    the worker thread, git's group is killed and the folder removed before it propagates."""
    import asyncio

    from agent_sessions import ask_orchestrator

    forge = tls("hang")

    async def go():
        task = asyncio.ensure_future(ask_orchestrator._fetch_repo(forge.url, None))
        deadline = time.monotonic() + 15
        while not forge.requests and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert forge.requests, "git never reached the server"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert pids and all(_group_gone(p) for p in pids)
    assert _left() == []
