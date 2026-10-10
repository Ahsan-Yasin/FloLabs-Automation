# Design: "Ink and proof", third edition

The website and app share one stylesheet, `web/static/css/site.css`. This file
explains the rules behind it so new pages stay consistent.

## The idea

The product copy-edits meeting transcripts. The layout is the second
edition's (centred hero with the product window, three steps and the
timeline, the zip manifest, the dark API band, questions, a centred close).
The third edition changes only the colours:

- **Neutral surfaces.** A white page (`--page`) with tinted bands
  (`--paper`) in light; near-black `#0a0a0b` in dark. Graphite ink
  (`--ink`), no navy anywhere.
- **One blue for the action.** `--accent` (`#0a62d0`) fills the main button
  of a view and the checked checkboxes, and draws the focus ring and input
  focus. Links stay ink with an underline.
- **The product's marks, content only.** A soft yellow wash
  (`--marker-soft`) behind transcript text that made the highlights reel, and
  solid yellow (`--marker`) only on small reel shapes. Cut text gets a grey
  strike (`--strike`) and a grey reason. Timelines are graphite
  (`--seg-kept`) with a grey hatch (`--hatch`) for cuts.
- **Signals.** Red (`--cut`) means failure or delete only; green (`--ok`)
  means done; amber (`--warn`) means attention.

## Rules

- **The marks are functional, never decoration.** Yellow and the red strike
  appear on transcripts, timelines, status and badges. Headlines carry no
  highlighter and no strike-through.
- **One blue button per view.** The main action is the blue filled button; the
  rest are outline or ghost. In the dark API band the button is white. Red is
  for delete and failure only.
- **Show the product.** The home page hero is the real job view (title,
  status, marked transcript, downloads) built from the app's own components,
  cut by the fold. No illustrations, no bar diagrams standing in for a
  screenshot.
- **No invented proof.** No customer logos, testimonials, avatars, star
  ratings or counters until real ones exist.
- **Sentence case everywhere**, including buttons, table headers and headings.
- **No decoration for its own sake.** No gradients on text, glows, blurred
  orbs, grid backgrounds, grain, or arrows on buttons. No middle-dot metadata
  strings: separate items with layout (gap) or commas.
- **Radii vary with size**: 4px badges, 8px buttons and inputs, 12px cards and
  tables, 16px sheets, plans and dialogs.
- **Layout varies by section.** On the home page no two neighbouring sections
  share a layout: centred hero, three steps in a row, a split with the file
  manifest, the dark band, a single narrow column, a centred close.
- **Headlines**: at most two lines on desktop and three on a phone, starting
  about a fifth of the way down the first screen. Hero subtext stays under 20
  words.
- **Type**: IBM Plex Sans for everything (400, 500, 600), IBM Plex Mono for
  timecodes, file names and code. Tabular numbers wherever numbers line up.
- **Boxes are for groups.** A panel holds a form, a list or a table. Single
  options, headings and table titles sit on the page without a box of their
  own.

## Surfaces

| Token | Light | Dark | Use |
|---|---|---|---|
| `--page` | `#ffffff` | `#0a0a0b` | The body background (the app uses `--paper`) |
| `--paper` | `#f4f4f5` | `#141416` | Tinted bands, recessed areas, table heads |
| `--paper-2` | `#ebebed` | `#2a2a2e` | Segmented controls, inline code, skeletons |
| `--surface` | `#ffffff` | `#1a1a1c` | Panels, inputs, menus, dialogs |
| `--ink`, `--ink-2`, `--ink-3` | `#17171a`, `#4a4a52`, `#66666e` | `#f2f2f3`, `#b4b4bc`, `#93939b` | Text: primary, secondary, tertiary |
| `--line`, `--line-2` | `#e4e4e7`, `#d4d4d8` | `#2c2c30`, `#3f3f46` | Hairlines, control borders |
| `--accent` | `#0a62d0` | `#0a62d0` (text `#5aa2ff`) | The main button, checked boxes, focus |
| `--marker`, `--marker-soft` | `#ffd43b`, `#fff1b8` | `#f2c63a`, yellow at 22 % | Reel shapes, highlighted text |
| `--cut`, `--ok`, `--warn` | `#c8102e`, `#1d7a3a`, `#a14f00` | `#ff6b61`, `#3ccf6e`, `#f5a524` | Failure/delete, done, attention |
| `--band`, `--band-2` | `#111113`, `#0a0a0b` | `#141416`, `#0a0a0b` | The dark API band and code blocks |

Every token has a dark value in `:root[data-theme="dark"]`. Use tokens, not hex
values, in templates and inline styles. Emails (`web/templates/email/`) use
the same palette inline.

## Motion

- **Controls, every page:** press feedback (buttons scale to 0.98), menus
  (150ms), dialogs (200ms), toasts (Motion, 200ms in and 150ms out) and colour
  changes on hover. Exits are faster than entrances; none runs past 300ms.
- **Pages, public site and sign-in only** (`web/static/js/motion.js`: GSAP,
  ScrollTrigger, SplitText, Lenis). The motion shows the product at work,
  plays once and ends on the page's normal styles:
  - On load the first screen rises in: the headline's lines out of a mask,
    then the sentence and the form, then the product window tilting up flat.
    About a second.
  - The transcript excerpts (home hero, sign-in aside) arrive plain and are
    marked line by line once in view. The hero's status reads "AI editing"
    until the last mark, then "Done", and the downloads appear.
  - Further down, content rises 24px into place the first time it scrolls
    into view, and lists arrive item by item.
  - The timeline ("before and after") is a small 3D stage (three.js,
    `web/static/js/cutline3d.js`, fetched as it nears the screen): the
    recording's blocks build up, the cuts fade back, copies of the kept parts
    lift off, travel into the final video and close up, and the highlights
    reel drops in first, while both timecodes count up. Each block takes its
    place and width from the flat bars, which stay in the page as the
    blueprint and the fallback (reduced motion, no WebGL), and its colours from
    the same tokens. It renders only while it changes. This is the one 3D
    element; the hero window's tilt is a plain CSS transform.
  - Lenis smooths wheel scrolling; in-page links glide (a keyboard press keeps
    the browser's own jump and focus move).
- **The app stays still:** no page motion and no smooth scrolling
  (`app/layout.html` empties the `motion` block).
- Nothing loops, follows the pointer, parallaxes or scrubs with the scroll.
- `prefers-reduced-motion: reduce` switches every animation, transition and
  the smooth scrolling off; the page is simply there.

## Components worth knowing

| Class | Use |
|---|---|
| `.hero-shot > .shot` | The job window on the home page (`.shot-bar`, `.shot-head`, `.shot-grid`, `.shot-panel`) |
| `.sheet`, `.line.is-kept`, `.line.is-cut` | A marked-up transcript excerpt (home and sign-in pages) |
| `.steps` | A numbered sequence; use only for real sequences |
| `.cutline`, `.reel` with `.x` and `.hl` spans, `.legend` | A timeline: hatched grey for cuts, yellow for highlights; with `data-scene` it is drawn in 3D over the bars (`.is-3d`, `.cutline-stage`) |
| `.files` | The zip's file manifest (icon, mono name, description) |
| `.section-head` | A section's heading and the sentence under it |
| `.split`, `.two-col` | Heading on the left, content on the right; one column below 960px |
| `.band` | The one dark section on the home page (API) |
| `.tint` | A tinted band on the white site |
| `.status status-<job status>` | Dot and label for job states; `.is-running` pulses |
| `.badge-keep`, `.badge-cut`, `.badge-ok`, `.badge-warn` | Small labels |
| `.check` | An option row with a checkbox, a name and one line of explanation |
| `.job-row` | A row in the dashboard's job list |
| `.stages .stage.done/.active/.failed` | The job progress list |
| `.table-wrap .table` | Tables scroll sideways inside their card below 560px |
| `.app-nav a[aria-current="page"]` | The current app page: a rule on the header's bottom edge |

## Accessibility checks

- Every page has a skip link, visible focus rings and labelled form fields.
- Controls are 40px tall with a mouse and 44px on touch screens
  (`@media (pointer: coarse)`).
- Icon-only controls carry an `aria-label`; the app nav keeps its labels for
  screen readers when it collapses to icons below 960px.
- Colour is never the only signal: cut lines are struck through, kept lines are
  highlighted and read normally, status pills have text.
- The job window on the home page is an example: its pretend controls are
  `aria-hidden`, and its transcript stays readable with "(cut: reason)" for
  screen readers.

## Where things live

- Templates: `web/templates/` (`base.html`, `partials/`, `pages/`, `auth/`, `app/`).
- Browser code: `web/static/js/core.js` (shared helpers, `window.HC`), one
  small script per app page, `motion.js` for the page motion of the public
  and sign-in pages, and `cutline3d.js` (an ES module) for the 3D timeline.
- Vendored libraries: Motion for toasts; GSAP (with ScrollTrigger and
  SplitText) and Lenis for page motion; three.js for the 3D timeline
  (`web/static/vendor/README.md`).
