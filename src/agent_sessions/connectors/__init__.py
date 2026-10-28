"""HQ connectors (#1746): services HQ reads from on the Commander's behalf, server side only.

P1a (#1750) is one read-only connector, Forgejo. The rules every connector follows are in
``docs/invariants/connectors.md``: a manifest only CHOOSES members of the closed sets in
:mod:`.kinds`; a credential is an AES-GCM envelope bound to its instance and credential revision
and sent only to the origin it was saved for; every request goes through ``connector_http``; and
third-party text reaches HQ guarded, capped and labelled as untrusted data.
"""
