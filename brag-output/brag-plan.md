# Brag Plan: Highlight Cutter

## What is this app?
Highlight Cutter takes a meeting recording (a YouTube link or an upload), has an AI editor mark every transcript sentence keep or cut with a reason, cuts the video frame-exact with ffmpeg, and hands back one zip: a final video that opens with the highlights, shorts, a video of everything removed, a report and YouTube chapters.

## The angle
The meeting, marked up like a manuscript. The product's own visual language is a copy editor's: struck-through text for what was cut, a yellow marker for what stays. The video uses exactly that, so a 1:38:12 recording visibly gets edited down to 41:05 in front of you. The humour is already in the product's demo data: "Sorry, one second, my dog is barking." gets struck out and labelled "interruption".

## Hook (first 2-3 seconds)
A huge monospace timecode `1:38:12` on a clean white page, labelled "Weekly product sync". A grey strike line slashes through it. One gesture says "this meeting is getting cut".

## Key moments (the middle)
- The hero input: a YouTube link types itself into the field and the "Cut this video" button gets clicked.
- The "What made the cut" transcript panel: lines arrive, then cut lines get struck through with their reason ("greeting", "interruption", "filler") while kept lines get the yellow marker sweep.
- The before/after reel: the Recording bar (kept segments + hatched cut segments) collapses into the Final video bar, highlights segment first; the timecode counts 1:38:12 → 41:05.
- The zip: the files land one by one (final.mp4, shorts/, removed.mp4, report.pdf, chapters.txt).

## Outro / punchline
"Stop watching meetings twice." Then the name, Highlight Cutter, and "Paste a YouTube link." on the dark band colour.

## User flow worth showing
Entry → key action → result:
1. Paste a YouTube link, press "Cut this video" (home hero form).
2. The AI editor marks up the transcript, sentence by sentence (job page "What made the cut").
3. "Done" → "Download everything": the zip with the final video, shorts, removed parts, report, chapters (job page Downloads).

## Tone
- Preset: default
- Creative direction: the meeting, marked up like a manuscript
- Interpretation: clean and warm, one idea per scene, comfortable holds; the strike-through and marker do the joking, the copy stays plain and quotes the product.

## Format: landscape — 1920x1080
## Duration: 23 seconds

## Visual identity (from the project)
- Background: #ffffff (page), #f4f4f5 (paper panels)
- Accent: #0a62d0 (primary button, links)
- Text: #17171a (ink), #66666e (ink-3, timecodes and notes)
- Marker: #ffd43b (reel highlight segment), #fff1b8 (highlighted kept text), #6b5200 (text on marker)
- Strike: #8a8a91 (line-through on cut words), hatch #a1a1aa (cut stripes on reels), kept segments #4a4a50
- Dark band (outro): #111113 with #ececef text
- Display font: IBM Plex Sans (600, tight letter-spacing -0.02em)
- Body font: IBM Plex Sans; timecodes and file names in IBM Plex Mono
- Strongest visual element: the transcript lines with strike-through + marker and reason labels on the right; the reel bars with hatched cut segments

## Share copy (draft)
A 1:38:12 meeting, marked up sentence by sentence and cut to 41:05, highlights first. The transcript is the edit.

## Audio direction
- Role: warm bed
- Music: happy-beats-business-moves-vol-1-by-ende-dot-app.mp3 (bundled)
- Music treatment: start at 0, bed around 0.32, gentle fade-in over the first 0.6s, fade out under the final logo, last 1.5s.
- Music cue guidance: bundled preset read (`assets/music/cues/happy-beats-business-moves-vol-1-by-ende-dot-app.music-cues.json`), tempo ≈120 BPM (beats 0.5s apart). Strong cues: 17.02s (zip list hits its stride), 20.02s (outro line "Stop watching meetings twice." lands). Beat grid for the zip files: every other beat from 16.02s (16.02, 17.02, 18.02, 19.02) so each file name holds ≥0.8s; all five then hold together.
- Audio-reactive treatment: subtle; the reel bars and the outro wordmark get a soft presence/glow from music RMS. No waveform or equalizer visuals.
- SFX posture: moderate, motion-matched, professional restraint (4-6 cues).
- Audio-coupled moments: per-character key ticks while the YouTube link types (thin them out), a click on "Cut this video", a soft swipe/slide as strike lines cross cut sentences (first and last only), card/drop sounds as zip files land (first and last accented), one bell-style payoff on the outro line.
- Restraint rule: no stacked hits, nothing harsh; no SFX on every transcript line; music never above 0.4.

## Storyboard

### Scene 1 — Hook: the duration — 2.8s
White page. A huge IBM Plex Mono `1:38:12` slams in centre, small label above: "Weekly product sync" (the site's demo meeting). At ~1.4s a grey strike line (#8a8a91) draws left to right through the timecode.
Sequential/interaction: label then timecode then the strike.
Audio intent: music swells in; the strike gets a soft swipe.
Audio-coupled idea: strike line draw = one soft slide sound.
Music: upbeat bed starting.
Transition mood: clean → Scene 2

### Scene 2 — Entry: paste a link — 3.2s
The home hero, recreated: headline "Your meeting recordings, cut to the parts worth watching." (verbatim, visible from the start of the scene, holds ≥2.7s), the URL input below it, a YouTube URL types in (`https://www.youtube.com/watch?v=…`, fictional id), then a cursor clicks the blue "Cut this video" button (#0a62d0), which presses down.
Sequential/interaction: yes — URL typed character by character, then simulated click.
Audio intent: light, tactile.
Audio-coupled idea: sparse key ticks for the typing, a click on the button.
Transition mood: clean wipe → Scene 3

### Scene 3 — The edit: what made the cut — 6.0s
The "What made the cut" panel (shot-panel styling: white card, 1px #e4e4e7 border, timecodes in mono on the left, speaker name above the line, reason label on the right). Four lines arrive in quick succession, then the editing happens:
- 0:00:04 Sam — "Can everyone hear me okay? Great, let's get started." → struck through, note "greeting"
- 0:03:12 Priya — "This quarter we shipped transcript reuse, which cut a third of our compute." → yellow marker sweep (kept)
- 0:03:31 Sam — "Sorry, one second, my dog is barking." → struck through, note "interruption"
- 0:04:40 Alex — "Uh, yeah, so, um, anyway." → struck through, note "filler"
All four lines settle and are fully visible together for ≥1.5s before the strikes start; strikes and the marker sweep go top to bottom ~0.45s apart; the final marked-up panel holds ≥1.2s. Kicker over the panel: "The transcript is the edit." (verbatim h2) fades in during the hold.
Sequential/interaction: yes — lines in one by one, then strike/marker applied one by one.
Audio intent: crisp, editorial.
Audio-coupled idea: soft slide on the first and last strike, a gentle marker pass on the kept line.
Transition mood: clean → Scene 4

### Scene 4 — Before and after — 3.5s
The two reel bars from the home page: "Recording" with kept segments (#4a4a50) and hatched cut segments, and its timecode 1:38:12. The hatched segments drop out and the kept ones slide together into the shorter "Final video" bar, which gains the yellow highlights segment at its front (#ffd43b); its timecode counts down 1:38:12 → 41:05. Caption (verbatim, trimmed): "The final video opens with the highlights, then plays the meeting without the cuts."
Sequential/interaction: yes — cut segments fall away, bar compacts, counter ticks.
Audio intent: satisfying compression.
Audio-coupled idea: soft impact when the final bar locks; optional tiny ticks on the counter (sparse).
Transition mood: clean → Scene 5

### Scene 5 — Everything in one zip — 4.5s
Header "Everything comes back in one zip." (verbatim). Under it the file list from the site, landing one by one with icons and the file names in mono:
final.mp4 · shorts/ · removed.mp4 · report.pdf · chapters.txt
Each name holds ≥0.8s; then all five hold together; the zip name `weekly_product_sync_8c1d2f07.zip` sits as the list header.
Sequential/interaction: yes — five file rows, every other beat from 16.02s.
Audio intent: arrival, payoff building.
Audio-coupled idea: card place/drop sound on the first and last row (beat-grid aligned).
Transition mood: soft → Scene 6

### Scene 6 — Outro — 3.0s
Dark band (#111113). "Stop watching meetings twice." lands at ~20.0s (beat-locked to the 20.02s strong cue) and holds; then "Highlight Cutter" in Plex Sans semibold with a short yellow marker underline, and "Paste a YouTube link." in #a3a3ab below. Music fades out under the last 1.5s.
Sequential/interaction: line, then wordmark.
Audio intent: confident landing.
Audio-coupled idea: one bell-style payoff on the line landing.

Scene durations: 2.8 + 3.2 + 6.0 + 3.5 + 4.5 + 3.0 = 23.0s

**Music mood for this video:** upbeat
**Audio summary:** an upbeat bed that starts with the strike on the timecode, stays light and tactile through the typing and the edit, builds as the zip files land on the beat, and lands one clean payoff on "Stop watching meetings twice." before fading out.

## Privacy note
All names and lines (Sam, Priya, Alex, "Weekly product sync", the zip name) are the site's own fictional demo data from `web/templates/pages/home.html`. No real users, keys, hosts or emails appear; the YouTube video id is a placeholder.
