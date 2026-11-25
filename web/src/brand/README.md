# BattleLab logo: the one source (#1590)

These SVGs are the BattleLab brand kit's own vector files, committed **verbatim**. The kit is in
the operator's Nextcloud at `BattleLab-Brand-Kit-Holo/`, and `brandAssets.test.ts` pins them by
sha256. Everything else that shows the logo is derived from this folder:

- **Favicons, icons and OG cards.** `node web/scripts/render-brand-assets.mjs` writes the favicon,
  the PWA icon sources, the maskable icon and the OG cards, plus the copies for the landing
  (`landing/`) and docs (`docs/site/public/`). It then rasterizes the PNGs the platforms need:
  manifest 192 and 512, maskable 512, apple-touch 180, and both OG cards at 1200×630.
- **Inline slots.** The same script fills each `<!-- brand:NAME -->…<!-- /brand:NAME -->` slot in
  the pages that load before sign-in or before the SPA. Those are the login, 2FA and
  change-password templates, `web/connect.html` and the landing header.
- **SPA components.** `components/brand/Brand.tsx` (`BrandMark`, `BrandWordmark`, `BrandLockup`) imports these files
  directly, so they ship from `/assets/`.

Change a file here, then run the script. The test fails while any derived copy or slot disagrees.

| File | Use |
|---|---|
| `mark-amber.svg` | **The primary icon:** favicon, app icon, beside a name in a bar. |
| `mark-dark.svg` | The icon on a light or busy ground. |
| `glyph-amber.svg` | The bare `>_` for tiny UI. |
| `logo-horizontal-notag-on-dark.svg` / `-on-light.svg` | Mark + wordmark when a tagline would be under 24 px tall. |
| `logo-horizontal-on-dark.svg` / `-on-light.svg` | Mark + wordmark + `// COMMAND & CODE`. |
| `wordmark-on-dark.svg` / `-on-light.svg` | The wordmark alone, outlined, beside a separate mark (the top bar, #1589). |

## Rules (the kit's)

- **Clear space:** 3× the height of the cursor bar on every side.
- **Minimum size:** the mark is 16 px; a horizontal lockup is 120 px wide, or 200 px with the tagline.
- **Never:** rotate it, add a gradient, round the corners, outline it, stretch it, or set the
  wordmark in another font.
- **Plate colours:** amber, dark, black or white only. The app's accent never recolours it.
- **No amber mark on an amber ground.** On light grounds, use the `-on-light` lockup.
