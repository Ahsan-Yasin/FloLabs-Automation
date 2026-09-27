# Design: "Ink and proof"

The website and app share one stylesheet, `web/static/css/site.css`. This file
explains the rules behind it so new pages stay consistent.

## The idea

The product copy-edits meeting transcripts. The interface borrows the two marks
an editor makes on paper and uses nothing else for colour:

- **A highlighter** (`--marker`, yellow) marks what is kept.
- **A red proofing strike** (`--cut`) marks what is removed.

Everything else is ink on paper: one dark navy ink (`--ink`) on a cool off-white
page (`--paper`), with white surfaces (`--surface`) for panels. The dark theme
swaps the page to deep navy and the ink to near-white; the two marks stay.

## Rules

- **One accent.** The highlighter appears only where something is kept or chosen:
  kept transcript lines, the kept words in the hero, the highlights segment of a
  timeline, the focus ring in dark mode. Buttons are ink, never yellow.
- **Red means removed or dangerous.** Cut text, failed status, delete buttons.
- **Sentence case everywhere**, including buttons, table headers and headings.
- **No decoration for its own sake.** No gradients on text, glows, blurred orbs,
  grid backgrounds, grain, or arrows on buttons. No middle-dot metadata strings:
  separate items with layout (gap) or commas.
- **Radii vary with size**: 4px badges, 6px buttons and inputs, 10px cards,
  14px sheets and dialogs.
- **Headlines**: at most two lines on desktop, starting about a quarter of the
  way down the first screen. Hero subtext stays under 20 words.
- **Type**: IBM Plex Sans for everything, IBM Plex Mono for timecodes, file
  names and code. Tabular numbers wherever numbers line up.

## The one signature detail

The home page headline contains a filler phrase ("um, so,") struck through in
red, and the kept words "worth watching" swept with the highlighter. It is pure
CSS (`strike-in` and `marker-in` keyframes, about one second in total), plays
once, reflows nothing, and the struck words are `aria-hidden` so screen readers
read the clean sentence. With reduced motion the final state shows at once.

Nothing else on the site animates on scroll.

## Motion

- Only press feedback (buttons scale to 0.98), menus (150ms), dialogs (200ms),
  toasts (Motion, 200ms in and 150ms out) and the hero edit.
- Exits are faster than entrances. Nothing runs longer than 420ms.
- `prefers-reduced-motion: reduce` switches every animation and transition off.

## Components worth knowing

| Class | Use |
|---|---|
| `.sheet`, `.line.is-kept`, `.line.is-cut` | A marked-up transcript excerpt (home and sign-in pages) |
| `.reel` with `.x` and `.hl` spans | A timeline: hatched red for cuts, yellow for highlights |
| `.files` | A file list with a mono name column (home "what comes back") |
| `.band` | The one dark section on the home page (API) |
| `.status status-<job status>` | Dot and label for job states; `.is-running` pulses |
| `.badge-keep`, `.badge-cut`, `.badge-ok`, `.badge-warn` | Small labels |
| `.job-row` | A row in the dashboard's job list |
| `.stages .stage.done/.active/.failed` | The job progress list |
| `.table-wrap .table` | Tables scroll sideways inside their card below 560px |

## Accessibility checks

- Every page has a skip link, visible focus rings and labelled form fields.
- Icon-only controls carry an `aria-label`; the app nav keeps its labels for
  screen readers when it collapses to icons below 960px.
- Colour is never the only signal: cut lines are struck through, kept lines are
  highlighted and read normally, status pills have text.

## Where things live

- Templates: `web/templates/` (`base.html`, `partials/`, `pages/`, `auth/`, `app/`).
- Browser code: `web/static/js/core.js` (shared helpers, `window.HC`) and one
  small script per app page. The public pages need no page script.
- Vendored library: Motion only, for toasts (`web/static/vendor/README.md`).
