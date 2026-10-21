# Fonts bundled with BattleLab (#1531)

Self-hosted, latin subset (the Google Fonts `latin` range), woff2. No CDN is contacted. The same
files ship on the public website (`landing/`, `landing/fonts-licenses.txt`).

| File | Family (as registered) | Upstream | Licence |
|---|---|---|---|
| `ChakraPetch-500.woff2`, `ChakraPetch-600.woff2`, `ChakraPetch-700.woff2` | `BattleLab Display` | Chakra Petch, Copyright The Chakra Petch Project Authors (Cadson Demak) | SIL Open Font License 1.1 |
| `JetBrainsMono-latin.woff2` (variable, 100–800) | `BattleLab Mono` | JetBrains Mono, Copyright The JetBrains Mono Project Authors | SIL Open Font License 1.1 |

**Why the families are renamed.** Registering the subset as `JetBrains Mono` would shadow an
operator's locally installed JetBrains Mono everywhere it is named, including the terminal font
picker, and replace it with a latin-only subset: box-drawing and other glyphs would then fall back
to a different face. Under its own name the bundled face only reaches the chrome that asks for it.

The SIL Open Font License 1.1 is at https://openfontlicense.org. Renaming the family in the
`@font-face` rule does not modify the font files (no Reserved Font Name is affected).
