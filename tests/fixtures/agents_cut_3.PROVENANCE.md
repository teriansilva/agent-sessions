# The published agents catalog cut 3, byte for byte

`agents_cut_3.plugin-feed.json` and `agents_cut_3.plugin-feed.json.sig` are copies of the signed agents cut that hosts and the catalog mirror read.

- **Fetched** 2026-11-24, anonymously, from the relay's retained publication: `https://relay.battlelab.superstatus.io/catalogs/agents/v1/releases/3-a41aace5d551496d41b965e222da33d2d9dbd21e8065c7d22b1c6ec2564d6352/plugin-feed.json` and `.sig`. The relay's `current.json` named this cut at the time.
- **Identity.** The file's sha256 is `a41aace5d551496d41b965e222da33d2d9dbd21e8065c7d22b1c6ec2564d6352`, the digest in the cut's own name. It is `sequence: 3`, issued 2026-11-13T00:01:58Z, expiring 2027-02-10T23:01:58Z, and hand-signed with the release key (#2082).
- **Unmodified.** Signed bytes can't be edited. The test checks the digest, so any change fails loudly.
- **Why it's here (#2321):** a stricter manifest rule (#2301) once made new code refuse this whole cut, which broke the catalog mirror and would have broken every upgraded host's saved catalog. Every cut that was ever published has to stay readable. Add each new published cut here when one is cut.
- **Freshness statement.** `agents_cut_3.freshness-current.json`, `agents_cut_3.freshness-statement.json` and `.json.sig` were fetched from `https://relay.battlelab.superstatus.io/catalogs/freshness/v1/agents/current.json` and that pointer's `releases/14-4c0266bd…/statement.json(.sig)` on the same day. That is statement 14, naming content sequence 3 and digest `a41aace5…`, valid 2026-11-23 to 2026-11-30. A long cut needs it to be accepted and to count as fresh. Tests pin their clock inside its window, so they never depend on the wall clock.
