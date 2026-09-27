# Vendored front-end libraries

Served from `/static/vendor/` so pages load no third-party scripts at runtime
(the Content-Security-Policy allows scripts from this site only). Pinned
versions, taken unmodified from the npm registry (`npm pack <name>@<version>`).

| File | Package | License | SHA-256 |
|---|---|---|---|
| `motion-13.4.4.js` | `motion@13.4.4`, `dist/motion.js` (UMD, global `Motion`) | MIT (`LICENSE-motion.md`) | `6d77ae5da8109c17f718d8b49929c7b748d34223ff216c409131e23459aea3df` |

Where they are used:

- Motion: toast enter and exit (every page). Everything else is plain CSS.

To upgrade: `npm pack motion@<v>`, copy the same `dist/` file with
the new version in the file name, update the table (hashes: `sha256sum`), and
change the script tag in `web/templates/base.html`.
