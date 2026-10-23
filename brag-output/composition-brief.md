# Hyperframes Composition Brief: Highlight Cutter

## Objective
Create a short launch-style brag video for Highlight Cutter.

## Output
- Composition directory: `brag-output/composition/`
- Rendered video: `brag-output/brag.mp4`
- Format: landscape — 1920x1080, 30 fps
- Duration: 23 seconds

## Source Material
- Project root: `D:\Coding\Python\FloLabs-Automation` (branch `prod`)
- Primary files read: `web/templates/pages/home.html` (hero, the "What made the cut" shot, reels, zip list, closing), `web/templates/app/job.html`, `web/templates/app/dashboard.html`, `web/static/css/site.css` (tokens, `.line`, `.reel`, `.shot`), `web/templates/base.html` (fonts), `README.md`
- Product name: Highlight Cutter
- Tagline / strongest claim: "Your meeting recordings, cut to the parts worth watching." / "The transcript is the edit."
- Key UI or visual moment to recreate: the "What made the cut" transcript panel (strike-through on cut lines with a reason label, yellow marker on kept lines) and the before/after reels (Recording 1:38:12 with hatched cuts → Final video 41:05 with the highlights segment first)
- Copy that must appear verbatim:
  - Your meeting recordings, cut to the parts worth watching.
  - Cut this video
  - What made the cut
  - The transcript is the edit.
  - Everything comes back in one zip.
  - Stop watching meetings twice.
  - Transcript lines (speaker, time, text, reason) exactly as in `home.html`

## Creative Direction
- Tone preset: default
- Creative direction: the meeting, marked up like a manuscript
- Interpretation: clean, warm, one idea per scene; generous holds on text; the strike-through and the marker carry the wit, the copy stays the product's own.
- Angle: a copy editor's red pen applied to a meeting. The product's own visual language (struck-through grey for cuts, yellow marker for kept lines, reason labels) edits a 1:38:12 recording down to 41:05, highlights first.
- Hook: huge mono `1:38:12` labelled "Weekly product sync", then a grey strike line draws through it.
- Outro / punchline: "Stop watching meetings twice." → Highlight Cutter wordmark with a marker underline → "Paste a YouTube link."
- Avoid:
  - Generic SaaS language
  - Abstract filler visuals
  - Unrelated visual redesign (keep the site's light, neutral, Plex look)

## Visual Identity
- Background: #ffffff (page); panels #f4f4f5 (paper); cards #ffffff with 1px #e4e4e7 border, radius 12-16px, soft shadow
- Text: #17171a (ink), #4a4a52 (ink-2), #66666e (ink-3)
- Accent: #0a62d0 (primary button, white text on it)
- Marker: #ffd43b shapes, #fff1b8 text wash, #6b5200 text on marker
- Strike: #8a8a91; hatch #a1a1aa; kept segments #4a4a50
- Outro band: #111113, text #ececef, secondary #a3a3ab
- Display font: IBM Plex Sans 600, letter-spacing -0.02em (needs @font-face; files in `assets/fonts/`)
- Body font: IBM Plex Sans 400/500; timecodes, URL and file names: IBM Plex Mono (bundled)
- Visual references from the project: `.line.is-cut` / `.line.is-kept` styling, `.reel` with hatched `.x` segments, `.shot` browser-window frame, `.files` list, primary button

## Storyboard
Use the storyboard in `brag-output/brag-plan.md` as the creative contract.

Scene summary:
1. Hook — 2.8s — `1:38:12` + "Weekly product sync", strike line through it
2. Entry — 3.2s — headline verbatim; URL types into the input; cursor clicks "Cut this video"
3. The edit — 6.0s — "What made the cut" panel, four lines, then strikes (greeting, interruption, filler) and one marker sweep; "The transcript is the edit."
4. Before and after — 3.5s — Recording reel collapses into Final video reel, highlights first; 1:38:12 → 41:05; caption
5. One zip — 4.5s — "Everything comes back in one zip." + five files land one by one
6. Outro — 3.0s — "Stop watching meetings twice." on the dark band, wordmark, "Paste a YouTube link."

## Audio
- Audio role: warm bed
- Audio arc: bed fades in under the strike, stays light under typing and editing, builds as the files land, one payoff on the outro line, fades under the wordmark.
- Music: `assets/music/happy-beats-business-moves-vol-1-by-ende-dot-app.mp3`
- Music treatment: volume ~0.32, fade-in 0.6s, fade-out over the last 1.5s
- Music cue guidance: bundled preset `C:\Users\Ahsan\.claude\skills\brag\assets\music\cues\happy-beats-business-moves-vol-1-by-ende-dot-app.music-cues.json`, ~120 BPM. Strong cues 17.02s and 20.02s (outro line lock). Zip rows on every other beat from 16.02s.
- Audio-reactive treatment: subtle — reel bars and the outro wordmark gain a soft presence from music energy; no waveform/equalizer visuals. If extraction is unavailable, skip and note it.
- Audio-coupled moments:
  - Scene 1 strike — one soft slide
  - Scene 2 typing — sparse key ticks; button — one click
  - Scene 3 strikes — first and last strike get a soft slide; marker pass on the kept line
  - Scene 4 final bar locks — soft impact
  - Scene 5 file rows — card/drop sound on first and last row
  - Scene 6 outro line — one bell-style payoff
- SFX selection guidance: few and exact, every sound has a cause on screen; prefer low high-frequency-risk files.
- SFX analysis guidance: `C:\Users\Ahsan\.claude\skills\brag\assets\sfx\sfx-analysis.md`
- Exact SFX choice: Hyperframes chooses filenames, timestamps, density and volume from the implemented animation.
- Audio files: copied into `brag-output/composition/assets/`

## Hyperframes Instructions
Use the current HyperFrames skills (`hyperframes` contract + format/motion/sound references, `hyperframes-cli` for lint/check/snapshot/render). /brag is its own workflow: no intent interview, no generic launch workflow.

Requirements:
- Show real UI and copy from the project (the transcript panel, the reels, the hero form, the file list).
- Keep all text readable in the final render.
- Keep the video at 23 seconds.
- Include the music bed and the planned SFX.
- `npx hyperframes check` passes before render.
- Keep creation and rendering local.
