"""BattleLab Home Free — home-side agent + end-to-end handshake.

The relay is blind: it pairs this agent with a browser and forwards opaque
frames. All confidentiality/authenticity lives here (:mod:`.handshake`) and in
the browser mirror. See ``docs/home-free-handshake.md`` for the wire spec.
"""
