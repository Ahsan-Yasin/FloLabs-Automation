# Vendored front-end libraries

Served from `/static/vendor/` so pages load no third-party scripts at runtime
(the Content-Security-Policy allows scripts from this site only). Pinned
versions, taken unmodified from the npm registry (`npm pack <name>@<version>`).

| File | Package | License | SHA-256 |
|---|---|---|---|
| `motion-13.4.4.js` | `motion@13.4.4`, `dist/motion.js` (UMD, global `Motion`) | MIT (`LICENSE-motion.md`) | `6d77ae5da8109c17f718d8b49929c7b748d34223ff216c409131e23459aea3df` |
| `gsap-3.15.0.min.js` | `gsap@3.15.0`, `dist/gsap.min.js` (global `gsap`) | GSAP Standard "no charge" license, https://gsap.com/standard-license | `92bb9a96476f983d212a2bc4f54c889039c1696dd4461d40a736860938570fbb` |
| `ScrollTrigger-3.15.0.min.js` | `gsap@3.15.0`, `dist/ScrollTrigger.min.js` (global `ScrollTrigger`) | same as GSAP | `b0b14d67b55b0c43c756ac0b106cfcb09d0879945f6ead64451065b0672916a2` |

Where they are used:

- Motion: reveal-on-scroll, the hero demo, micro-interactions (every public page).
- GSAP + ScrollTrigger: the pinned "How it works" section (home page only).

To upgrade: `npm pack motion@<v> gsap@<v>`, copy the same `dist/` files with
the new version in the file name, update the table (hashes: `sha256sum`), and
change the script tags in `web/templates/base.html` / `pages/home.html`.
