# Vendored third-party assets

Everything the dashboard loads from the browser lives here, as a file, in
the repository. Nothing is fetched from a CDN at run time: a plant network
usually has no route to the internet, and an install that only works when
Cloudflare is reachable is not an install. It is also why there is no
JavaScript build step.

Each entry records what it is, where it came from and the SHA-256 of the
exact bytes committed, so that "what is in this build" has an answer that
does not depend on anyone's memory. Re-verify with:

```bash
sha256sum packages/core/src/jetpace_core/web/static/vendor/*
```

| File | Version | Licence | SHA-256 |
|---|---|---|---|
| `echarts.min.js` | 6.1.0 | Apache-2.0 | `b66b25aeb4df84e33199dc21694014d336d222cbd9deb0e5a7c14bd6aa0d0fd0` |
| `htmx.min.js` | 2.0.4 | BSD-2-Clause (Zero-Clause upstream) | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `inter-latin.woff2` | Inter v20, latin subset | SIL OFL 1.1 | `3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62` |
| `inter-LICENSE.txt` | — | — | `262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a` |

All three permit commercial redistribution, which is a CLAUDE.md invariant
and not a formality — this software is sold.

## Inter

Source: the `latin` subset served by Google Fonts for
`https://fonts.googleapis.com/css2?family=Inter:wght@400..700`, which
resolves to
`https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa1ZL7.woff2`.
Downloaded 14 October 2026.

**One variable file, not four static weights.** The `wght` axis runs
100–900, so every weight the design uses comes out of 48 KB. Asking Google
for `400..700` only narrows what the CSS declares, not what the file
contains.

**The `latin` subset only.** It covers U+0000–00FF plus the typographic
characters the templates actually use — `&mdash;`, `&middot;`, the minus
sign, the currency block. Latin-ext, Greek, Cyrillic and Vietnamese were
not taken; if a site name ever needs them, add that subset as a second
`@font-face` with its own `unicode-range` rather than swapping this file
for a larger one.

**Verified on arrival** rather than assumed: `wOF2` magic bytes, and the
OpenType feature list contains `tnum`. That last one is the engineering
reason this font is here at all — tabular figures keep a column of
readings aligned and stop a changing value making its row twitch, and the
`system-ui` stack does not reliably provide them on Windows.

The OFL requires the licence to travel with the font, which is why
`inter-LICENSE.txt` sits beside it. It must not be removed, and the font
must not be sold on its own; bundling it with this software is expressly
permitted.

## Replacing any of these

Download the new file, check its bytes are what you expect, update the
table above, and commit the two together. A vendored asset whose hash is
not written down is one nobody can audit later.

`.gitattributes` marks this directory `-text`, so git never rewrites a
byte of these files on checkout. That matters: a line-ending conversion
inside a `.woff2` corrupts it silently, and inside a minified bundle it
changes every recorded hash for no reason.
