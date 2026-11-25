"""The closed value space a connector manifest selects from (#1746 D1, #1750).

Every set here is **reviewed code**, as in ``plugins/kinds.py``: a manifest may only *choose* a
member, never add one. A new protocol is a PR that adds an adapter kind (and its adapter); a
service that speaks an existing protocol is a new manifest only. P1a adds only what Forgejo needs;
later kinds are added by their own children, each named beside the member it needed.
"""

from __future__ import annotations

#: Manifest contract versions this build reads. A higher contract is refused, never half-read. An
#: ADDITIVE field (e.g. P2's `send`) does not bump it: an older build meets it as an unknown field
#: and refuses the manifest, which is the same fail-closed outcome (the plugin manifest's rule).
CONTRACT_CURRENT = 1

#: `adapter.kind`: the reviewed code that talks to the service. `forgejo-api` (#1750) also serves
#: Gitea and Codeberg, which speak the same `/api/v1`.
#: `github-rest` (#1826) reads github.com's REST API. `nextcloud` (#1829) reads a Nextcloud's files
#: over WebDAV (`PROPFIND`, `GET`) and searches them through its unified search.
#: `telegram-bot` and `matrix-cs` (#1832) are messengers: they read nothing, they list who the bot
#: can reach, and (#1833) send what the Commander taps. `smtp` (#1836) sends mail through the
#: Commander's own account, over a separately reviewed transport (`connector_smtp`, never HTTP).
ADAPTER_KINDS = frozenset(
    {"forgejo-api", "github-rest", "nextcloud", "telegram-bot", "matrix-cs", "smtp"}
)

#: `auth.kind`. `token-header`: a personal access token, sent by `connector_http` only, as
#: ``Authorization: <scheme> <token>``, only to the origin it was saved for. `basic` (#1829): an
#: ACCOUNT name (plain, stored on the instance) plus a secret (an app password, stored like a
#: token), sent by `connector_http` only, as ``Authorization: Basic base64(account:secret)``.
#: `path-token` (#1832): Telegram's bot token, the URL's first path segment (`/bot<token>/…`),
#: written into the path by `connector_http` only, after vetting.
AUTH_KINDS = frozenset({"token-header", "basic", "path-token"})

#: `auth.scheme`. Forgejo's own scheme is `token`; GitHub's is `Bearer`; `basic` is always `Basic`.
AUTH_SCHEMES = frozenset({"token", "Bearer", "Basic", "bot"})

#: Which schemes each auth kind may name.
AUTH_KIND_SCHEMES = {
    "token-header": frozenset({"token", "Bearer"}),
    "basic": frozenset({"Basic"}),
    "path-token": frozenset({"bot"}),
}

#: `auth.scopes`: what the Commander is told to grant the token (shown, never enforced here).
#: GitHub's are the fine-grained token permissions, read-only (#1826).
AUTH_SCOPES = frozenset(
    {
        "read:user",
        "read:issue",
        "read:repository",
        "contents:read",
        "issues:read",
        "pull_requests:read",
        "commit_statuses:read",
        "actions:read",
        "app-password",
        "bot-token",
        "access-token",
    }
)

#: `identity.category` (the gallery's sections, #1746). `storage` (#1829): files and folders.
#: Mail (#1836) is a `messenger`: it sends what the Commander taps, to recipients they allowed.
CATEGORIES = frozenset({"versioning", "storage", "messenger"})

#: `capabilities.read`: what HQ may read through the adapter, per category. A forge's `files` are
#: files in a repository at a commit; storage's `documents` are files in the Commander's own store.
VERSIONING_READS = frozenset({"repos", "issues", "pulls", "ci", "files"})
STORAGE_READS = frozenset({"folders", "search", "documents"})
READ_OPS = VERSIONING_READS | STORAGE_READS

#: `capabilities.send` (#1832): what HQ may PROPOSE through the adapter; every send waits for a tap
#: (#1833). A manifest may have no reads only when it has a send.
SEND_OPS = frozenset({"message"})

#: `origin.mode`. `operator`: the Commander enters the origin (self-hosted Forgejo). `fixed`: the
#: manifest names it (a hosted service: GitHub, #1826).
ORIGIN_MODES = frozenset({"operator", "fixed"})

#: `identity.publisher`. P1a ships first-party manifests only.
PUBLISHERS = frozenset({"battlelab"})

#: Which adapter each read op needs: a manifest may not claim a read its adapter cannot do.
ADAPTER_READS = {
    "forgejo-api": VERSIONING_READS,
    "github-rest": VERSIONING_READS,
    "nextcloud": STORAGE_READS,
    "telegram-bot": frozenset(),
    "matrix-cs": frozenset(),
    "smtp": frozenset(),
}

#: Which adapters may send, and what.
ADAPTER_SENDS = {"telegram-bot": SEND_OPS, "matrix-cs": SEND_OPS, "smtp": SEND_OPS}

#: The longest message each sending adapter takes (#1833), in characters: Telegram's own limit;
#: Matrix has no per-message limit below its 64 KiB event cap, so a conservative bound. Mail
#: (#1836): a text/plain body, also capped on the wire after encoding (`connector_smtp`).
SEND_MAX_CHARS = {"telegram-bot": 4096, "matrix-cs": 32000, "smtp": 32000}

#: Adapters whose provider deduplicates a repeated send (#1833, #1746 D7): Matrix keys a send by
#: its txnId, so one claim may repeat it once after an ambiguous end. Never after an interruption.
#: SMTP is not idempotent: anything ambiguous after `DATA` is `unconfirmed`, never resubmitted.
IDEMPOTENT_SENDS = frozenset({"matrix-cs"})

#: The origin schemes each adapter's origin may use. A kind with no row takes `http(s)` (the
#: store's rules). `smtp` (#1836): `smtps` (implicit TLS, port 465 unless stated) or `smtp`
#: (STARTTLS that must succeed, port 587 unless stated). Never `http(s)` for mail, never `smtp`
#: for anything else.
ORIGIN_SCHEMES = {"smtp": frozenset({"smtps", "smtp"})}
HTTP_SCHEMES = frozenset({"http", "https"})

#: Adapters whose recipients the Commander TYPES (#1836): nothing to discover, so no candidates.
TYPED_RECIPIENTS = frozenset({"smtp"})

#: Which category each adapter serves: a manifest's `identity.category` must be its adapter's.
ADAPTER_CATEGORY = {
    "forgejo-api": "versioning",
    "github-rest": "versioning",
    "nextcloud": "storage",
    "telegram-bot": "messenger",
    "matrix-cs": "messenger",
    "smtp": "messenger",
}

#: Which auth each adapter speaks.
ADAPTER_AUTH = {
    "forgejo-api": frozenset({"token-header"}),
    "github-rest": frozenset({"token-header"}),
    "nextcloud": frozenset({"basic"}),
    "telegram-bot": frozenset({"path-token"}),
    "matrix-cs": frozenset({"token-header"}),
    "smtp": frozenset({"basic"}),
}
