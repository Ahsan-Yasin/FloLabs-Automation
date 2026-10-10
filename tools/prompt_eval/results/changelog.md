# Prompt tuning changelog (decide/prompts.py), 2026-10-05/06

TRAIN split only (w01 w02 w03 w04 w08 w10), Claude Sonnet 5.5, every run with `--rerank`.
The test split was not run or read.
Baseline train = 91.21 (from the `baseline` all-split run, before the format penalty;
that run's 2 format problems were not tied to a window).

| round | tag   | score | must-keep cut | junk kept | format | top found | removal F1 | hl_concord | top_f1 | short_f1 | cost    | verdict |
|-------|-------|-------|---------------|-----------|--------|-----------|------------|------------|--------|----------|---------|---------|
| base  | baseline (train part) | 91.21* | 1/40 | 36/235 | (2 overall) | 3/6 | 0.910 | 0.953 | 0.667 | 1.000 | (0.1422 all) | - |
| 1     | tune1 | 91.58 (93.58 before -2) | 0/40 | 29/235 | 1 | 4/6 | 0.915 | 0.942 | 0.800 | 1.000 | $0.0893 | KEEP |
| 2     | tune2 | 93.78 | 0/40 | 12/235 | 0 | 3/6 | 0.962 | 0.923 | 0.667 | 1.000 | $0.0780 | KEEP |
| 3     | tune3 | **94.88** | 0/40 | 10/235 | 0 | 4/6 | 0.968 | 0.958 | 0.800 | 0.800 | $0.0828 | KEEP (final) |
| 4     | tune4 | 94.75 | 0/40 | 12/235 | 0 | 4/6 | 0.964 | 0.959 | 0.800 | 0.800 | $0.0770 | REVERTED |

*before the format penalty.

## Round 1 (kept): junk kept + must-keep lead-in + format
- New remove bullet: talk about the meeting rather than the work (who is missing/late, leaving,
  rescheduling, availability, setting up another call or a one-to-one, "let me know") is
  housekeeping, never a decision, even when the meeting has nothing else in it.
- New remove bullet: thanks, goodbyes, apologies, welcomes, reassurance ("it's okay") are
  removed, also between kept lines.
- New keep bullet: a line that opens a real item or gives a work instruction is content; a
  bare check ("did you hear me?") is not.
- "Never cut a sentence in half" now covers only the rest of that sentence; stray words, bare
  numbers, "okay?" and filler beside a kept thought are still removed.
- Output rule: every line has all five fields; use only the listed codes.
- Effect: must-keep cut 1 -> 0, junk kept 36 -> 29. But w01's Toggl task-naming went from 2 to
  12 junk lines kept, because my example "please open your timesheet" pushed toward keeping it.
  There was 1 format problem ("0 r greet 0 house -").

## Round 2 (kept): w01 setup housekeeping + format order
- Removed the timesheet example. Added: setting up for this meeting on screen (logging in,
  opening or sharing a file, naming or logging this meeting in a tracker) is housekeeping, and
  so are fragments that never become a sentence.
- Output rule now gives the field order: number, k or r, score (a number), removal code or -,
  highlight code or -.
- Effect: w01 junk 12 -> 0, total junk 29 -> 12, format problems 1 -> 0.

## Round 3 (kept): highlight scoring
- Judge anchors: 6-7 now also covers "a decision or opinion with its reason, a root cause
  found". 8-10 notes that a clear teaching moment belongs there. Small talk, thanks and
  scheduling score 0-2 even inside a good stretch.
- Rerank: "a decision, opinion or root cause stated with its reason is worth at least 5, even
  when short"; score the best point in a candidate, not the small talk around it; the clip a-b
  leaves out small talk, thanks and scheduling at either end.
- Effect: hl_concord 0.923 -> 0.958, top found 3 -> 4 of 6 (w10 Claude/Cursor opinion raw 5),
  w04 Jetson peak 7. short_f1 fell 1.0 -> 0.8 because the w10 opinion clip was marked short
  (gold: no shorts in w10).

## Round 4 (reverted): shorts precision + root-cause wording (rerank only)
- Tried: "the real cause of a problem once it is found" and "n for advice or an opinion that
  explains nothing". The w10 root cause stayed at raw 4 and the opinion clip was still a short.
  The score went 94.88 -> 94.75 (judge noise). Reverted to the round-3 prompts.

## Not fixable in the prompt
- The w04[49-53] decision (switch to enterprise remote access) is scored 5-6 per line. But
  lines 11-56 all score 3 or more and merge into ONE candidate in edl/highlights.candidate_moments,
  so the rerank can return only one clip (13-32). That needs a pipeline change (split long
  candidates), not a prompt change.
- Gold labels disagree on status questions (w10[20-21] remove vs w02[23], [32] keep). These
  remain the leftover errors.

## Tests
`pytest tests/test_decide.py tests/test_chapters.py tests/test_anthropic.py` with empty keys:
97 passed. No test pins the changed wording ("LEARN", "genuinely funny", "learn from",
"plain status" and "knowing something new" are still present).
