"""The forge WRITES a playbook publication needs, and nothing else (#1196).

`forge.py` is read-only by construction and its test asserts that over the public surface; that
property stays where it is. The two writes a publication needs live here instead, in a separate
class, so the read adapter never grows a `post()`:

* **create a repository** (`create_repo`), only when the operator asked for one through a
  `forge` connection and the plan showed the target first;
* **open a pull request** (`create_pull`).

**There is no delete path, no update path and no merge path.** Not "we do not call one": the
class defines none, `_send` refuses every method but `GET` and `POST`, and
`tests/test_playbook_git.py` asserts both. Deleting a remote repository is out of scope forever.

The credential is resolved by the caller from the scoped-variable store at the moment of the
call (a project's secret binding, or its explicit reference to a global one) and handed in; it is
sent as a header and never placed in a URL, an argv, an error or a log. `_why` names an
exception's kind only, exactly like `forge._why`.

This is an outbound-HTTP capability, so it is inventoried in `tests/test_prompts_registry.py`:
`HTTP_MODULES` names this module and `POST_SITES` counts its single call site.
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote, urlsplit

import httpx

from . import forge

RESPONSE_MAX = forge.RESPONSE_MAX
READ_CHUNK = forge.READ_CHUNK
#: The only methods `_send` will issue. A DELETE (or PATCH, PUT) is refused before any request.
METHODS = frozenset({"GET", "POST"})

#: Test seam, mirroring `forge.set_transport_for_test`; CI never reaches a network.
_TRANSPORT: httpx.BaseTransport | None = None


def set_transport_for_test(t: httpx.BaseTransport | None) -> None:
    global _TRANSPORT
    _TRANSPORT = t


class ForgeWriteError(RuntimeError):
    """The forge refused or could not be asked. `status` is the forge's HTTP answer, if any."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def base_authority(base_url: str) -> tuple[str, str, int]:
    """`(scheme, host, port)` of an API base URL, refusing anything a credential must not reach.

    HTTPS only: a token on a plaintext endpoint is a token on the wire. No userinfo, query or
    fragment, the same rules `prefs.validate_forge_patch` applies to the Settings forge.
    """
    base = (base_url or "").strip().rstrip("/")
    try:
        parts = urlsplit(base)
        port = parts.port
    except ValueError:
        raise ForgeWriteError("the forge connection is not a URL") from None
    if parts.scheme != "https" or not parts.hostname:
        raise ForgeWriteError("the forge connection must be an https URL")
    if parts.username or parts.password or "@" in parts.netloc or parts.query or parts.fragment:
        raise ForgeWriteError("the forge connection may not carry a credential, query or fragment")
    return parts.scheme, parts.hostname.lower(), port or 443


def web_host(kind: str, base_url: str) -> str:
    """The host git remotes of this forge use. GitHub's API lives on `api.<host>`."""
    _, host, _ = base_authority(base_url)
    if kind == "github" and host.startswith("api."):
        return host[4:]
    return host


class ForgeWriter:
    """Create a repository, find or open a pull request. **No method deletes anything.**"""

    KINDS = forge.ForgeClient.KINDS

    def __init__(self, *, kind: str, base_url: str, token: str) -> None:
        if kind not in self.KINDS:
            raise ForgeWriteError(f"unknown forge kind {kind!r}")
        base_authority(base_url)
        if not token:
            raise ForgeWriteError("the forge connection has no credential bound")
        self.kind = kind
        self.base = base_url.strip().rstrip("/")
        self._token = token

    @property
    def _api(self) -> str:
        return "" if self.kind == "github" else "/api/v1"

    @staticmethod
    def _seg(s: str) -> str:
        return quote(str(s), safe="")

    def _headers(self) -> dict[str, str]:
        auth = f"Bearer {self._token}" if self.kind == "github" else f"token {self._token}"
        return {"Accept": "application/json", "Authorization": auth}

    def _send(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        """THE one outbound call in this module. Counted in `POST_SITES`.

        Same discipline as `forge.ForgeClient._get`: `trust_env=False`, TLS verification at its
        default, no redirects (a 30x would carry the token to an authority nobody configured),
        a streamed body capped at `RESPONSE_MAX` in `READ_CHUNK` pieces, and a total deadline.
        """
        if method not in METHODS:
            raise ForgeWriteError(f"{method} is not a request this module makes")
        url = f"{self.base}{self._api}{path}"
        budget = forge.request_timeout()
        deadline = time.monotonic() + budget
        with httpx.Client(
            timeout=budget, transport=_TRANSPORT, trust_env=False, follow_redirects=False
        ) as client:
            with client.stream(method, url, json=body, headers=self._headers()) as r:
                chunks: list[bytes] = []
                size = 0
                for chunk in r.iter_bytes(chunk_size=READ_CHUNK):
                    if time.monotonic() > deadline:
                        raise ForgeWriteError("the forge took too long to answer")
                    size += len(chunk)
                    if size > RESPONSE_MAX:
                        raise ForgeWriteError(f"the forge answered more than {RESPONSE_MAX} bytes")
                    chunks.append(chunk)
                status = r.status_code
        if 300 <= status < 400:
            raise ForgeWriteError(
                f"the forge redirected ({status}); point the connection at the final address",
                status,
            )
        if status in (401, 403):
            raise ForgeWriteError("the forge refused the credential", status)
        raw = b"".join(chunks)
        if status >= 400:
            return status, None
        try:
            return status, json.loads(raw) if raw.strip() else None
        except ValueError:
            raise ForgeWriteError("the forge answer was not JSON", status) from None

    # ------------------------------------------------------------------ reads the writes need

    def login(self) -> str:
        status, body = self._send("GET", "/user")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("login"), str):
            raise ForgeWriteError(f"the forge did not say who the credential is ({status})", status)
        return body["login"]

    def repo(self, owner: str, name: str) -> dict | None:
        """The repository, or None when the forge answers 404. Anything else refuses."""
        status, body = self._send("GET", f"/repos/{self._seg(owner)}/{self._seg(name)}")
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict):
            raise ForgeWriteError(f"the forge answered {status} for the repository", status)
        return body

    def ssh_endpoint(self) -> str:
        """The forge's OWN ssh address prefix, read from a repository it already advertises.

        Neither Forgejo nor GitHub exposes its ssh domain or port as a setting, but every
        repository answer carries `ssh_url` (`ssh://git@host:2222/o/n.git`, `git@host:o/n.git`,
        a separate ssh domain, a sub-path...). One repository the credential can see is read and
        its `<owner>/<name>.git` tail removed; what is left is how THIS forge writes an ssh
        address, port included. No visible repository, or an answer whose tail is not its own
        full name, refuses: the address is never guessed.
        """
        status, rows = self._send("GET", "/user/repos?limit=1&per_page=1&page=1")
        if status != 200 or not isinstance(rows, list):
            raise ForgeWriteError(f"the forge answered {status} listing repositories", status)
        for row in rows[:1]:
            full = row.get("full_name") if isinstance(row, dict) else None
            url = row.get("ssh_url") if isinstance(row, dict) else None
            if isinstance(full, str) and isinstance(url, str):
                tail = f"{full}.git"
                if (
                    len(url) > len(tail)
                    and url.lower().endswith(tail.lower())
                    and url[-len(tail) - 1] in "/:"
                ):
                    return url[: -len(tail)]
        raise ForgeWriteError(
            "the forge's ssh address cannot be determined (the credential sees no repository "
            "that advertises one); nothing was created"
        )

    def open_pull(self, owner: str, name: str, *, head: str, base: str) -> dict | None:
        """The OPEN pull request whose head is `head` in this same repository, if one exists.

        Identity is the head branch AND the head repository's full name, as in `forge`: a
        same-named branch on a fork is somebody else's pull request.
        """
        full = f"{owner}/{name}".lower()
        for page in range(1, 7):
            status, rows = self._send(
                "GET",
                f"/repos/{self._seg(owner)}/{self._seg(name)}/pulls"
                f"?state=open&limit=50&per_page=50&page={page}",
            )
            if status != 200 or not isinstance(rows, list):
                raise ForgeWriteError(f"the forge answered {status} listing pull requests", status)
            for pr in rows:
                head_info = pr.get("head") if isinstance(pr, dict) else None
                base_info = pr.get("base") if isinstance(pr, dict) else None
                if not isinstance(head_info, dict) or head_info.get("ref") != head:
                    continue
                repo_info = head_info.get("repo")
                if not isinstance(repo_info, dict):
                    continue
                if str(repo_info.get("full_name") or "").lower() != full:
                    continue
                if isinstance(base_info, dict) and base_info.get("ref") not in (None, base):
                    continue
                return pr
            if len(rows) < 50:
                return None
        raise ForgeWriteError("more than 300 open pull requests; the search did not finish")

    # ------------------------------------------------------------------ the two writes

    def create_repo(self, owner: str, name: str, *, private: bool, description: str) -> dict:
        """Create `owner/name`. An existing name is refused by the forge (409/422) and here."""
        me = self.login()
        path = "/user/repos" if owner.lower() == me.lower() else f"/orgs/{self._seg(owner)}/repos"
        status, body = self._send(
            "POST",
            path,
            {"name": name, "private": private, "description": description, "auto_init": False},
        )
        if status in (409, 422):
            raise ForgeWriteError("a repository of that name already exists", status)
        if status not in (200, 201) or not isinstance(body, dict):
            raise ForgeWriteError(f"the forge answered {status} creating the repository", status)
        return body

    def create_pull(
        self, owner: str, name: str, *, head: str, base: str, title: str, body: str
    ) -> dict:
        status, out = self._send(
            "POST",
            f"/repos/{self._seg(owner)}/{self._seg(name)}/pulls",
            {"head": head, "base": base, "title": title, "body": body},
        )
        if status not in (200, 201) or not isinstance(out, dict):
            raise ForgeWriteError(f"the forge answered {status} opening the pull request", status)
        return out


def why(e: Exception) -> str:
    """What to tell the operator: an authored message, or only the exception's kind."""
    if isinstance(e, ForgeWriteError):
        return str(e)
    return f"forge unreachable ({type(e).__name__})"
