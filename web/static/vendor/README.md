# Vendored front-end libraries

Served from `/static/vendor/` so pages load no third-party scripts at runtime
(the Content-Security-Policy allows scripts from this site only). Pinned
versions, taken unmodified from the npm registry (`npm pack <name>@<version>`).

| File | Package | License | SHA-256 |
|---|---|---|---|
| `motion-13.4.4.js` | `motion@13.4.4`, `dist/motion.js` (UMD, global `Motion`) | MIT (`LICENSE-motion.md`) | `6d77ae5da8109c17f718d8b49929c7b748d34223ff216c409131e23459aea3df` |
| `gsap-3.15.0.min.js` | `gsap@3.15.0`, `dist/gsap.min.js` (UMD, global `gsap`) | GSAP standard no-charge license, https://gsap.com/standard-license | `92bb9a96476f983d212a2bc4f54c889039c1696dd4461d40a736860938570fbb` |
| `gsap-scrolltrigger-3.15.0.min.js` | `gsap@3.15.0`, `dist/ScrollTrigger.min.js` (global `ScrollTrigger`) | as GSAP | `b0b14d67b55b0c43c756ac0b106cfcb09d0879945f6ead64451065b0672916a2` |
| `gsap-splittext-3.15.0.min.js` | `gsap@3.15.0`, `dist/SplitText.min.js` (global `SplitText`) | as GSAP | `419f7027a5f086a12cb7988736d8fdd3a6ed2200229661de25b6628ca7ced344` |
| `lenis-1.3.26.min.js` | `lenis@1.3.26`, `dist/lenis.min.js` (global `Lenis`) | MIT (`LICENSE-lenis.md`) | `53195c9797e7ce7bf9d7fa9242b08209e57f46de4c9dac126a6494fa780e3346` |
| `three-0.186.1.min.js` | `three@0.186.1`, `build/three.module.js` (ES module) bundled into one minified file, see below | MIT (`LICENSE-three.md`) | `1c5931a54b06ef6f20cf2ed10a7ff1c3ae257d941f965894470b5cd02075e7cf` |

three.js stopped shipping minified builds, so its file is the one exception to
"unmodified": `npx esbuild@0.28.2 build/three.module.js --bundle --format=esm
--minify --legal-comments=inline --outfile=three-0.186.1.min.js`, run inside
the unpacked package (source hashes: `build/three.module.js`
`9052042d676cb0fdc1ddfefe193053f34b7ac0513a616fdac4535d49987812ea`,
`build/three.core.js`
`9edde002b066a9a05676a6127f67735b62baf399bdea529f2f7e31657da769e6`).

React Bits (github.com/DavidHDev/react-bits) ships React source to copy, not a
package, and this site has no React. `web/static/js/motion.js` carries small
vanilla ports of four of its patterns on top of GSAP: SplitText, AnimatedContent,
AnimatedList and CountUp. Its license (MIT + Commons Clause) is in
`LICENSE-react-bits.md`.

Where they are used:

- Motion: toast enter and exit (every page).
- GSAP, ScrollTrigger, SplitText, Lenis: page motion on the public site and the
  sign-in pages (`js/motion.js`, loaded by the `motion` block in
  `web/templates/base.html`; the app layout leaves the block empty).
- three.js: the 3D timeline on the home page (`js/cutline3d.js`, imported
  by `motion.js` only as the timeline nears the screen).
  Everything else is plain CSS.

To upgrade: `npm pack <name>@<v>`, copy the same `dist/` file with
the new version in the file name, update the table (hashes: `sha256sum`), and
change the script tag in `web/templates/base.html`.
