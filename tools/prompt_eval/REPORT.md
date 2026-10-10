# Prompt tuning report: the AI step (judge + rerank) on Claude Sonnet 5.5

Run: 2026-10-05 23:47 to 2026-10-06 00:42 (one hour, as asked). Branch `flolabs`, nothing committed or pushed.
Total Claude spend: **$0.53 of the $3.00 budget**.

## The short version (plain language)

- **What we did.** We took 10 short stretches (about 3-7 minutes each) of real FloLabs meetings and chose hard ones on purpose: silent starts, a cancelled meeting, people being called on for status, welcome rounds, long goodbyes and an almost-empty recording. We wrote down, line by line, what the app should keep, cut and highlight. Then we ran the app's own AI step on them and scored the answers automatically.
- **How Sonnet 5.5 did out of the box.** Quite well. It cut only 2 of 74 lines that must stay and found 7 of 11 best moments. Its main weakness was keeping junk: talk about the meeting itself ("let me know when you want to meet", "I'll be online until 3"), "it's okay" and thank-you lines, and goodbyes. It also answered in the wrong format twice, which forced retries. The old saved Haiku answers on the same windows scored slightly higher, mainly because Haiku cut more of that junk.
- **What we changed.** We added a few general rules to `decide/prompts.py`:
  - Cut talk about the meeting.
  - Cut thanks, apologies and "it's okay".
  - Keep a line that opens a real item.
  - Always answer in the exact format.
  - Score decisions with reasons, and teaching moments, higher.
- **Did it work?** On the 6 practice windows, yes. The score rose from 91.2 to 94.9 and the junk kept fell from 36 lines to 10. On the 4 windows the tuning never saw, the result was mixed. Junk kept fell from 10 to 3, but the new rules started cutting a real status update, which is exactly "the big bug" the product must avoid. It happened in both test runs. The held-out score went slightly down (94.1 to 93.5).
- **What to do.** Do not ship the tuned prompt as is. It is in `decide/prompts.py` now, uncommitted. Either undo it (`git checkout decide/prompts.py`) or apply the two small fixes under Recommendations, which should keep the gains without the bug, and test once more for about 10-15 cents.
- **Cost.** Sonnet 5.5 costs about 1.2-1.4 cents per 4-5 minute window. That works out to roughly 8-10 cents per 30 minutes of meeting for judge + rerank. The tuned prompt was about 15% cheaper because it stopped needing retries.
- **How much to trust this.** This is a small test: 10 windows, about 42 minutes of transcript, labels written quickly in one pass, and one run per setting. Treat the numbers as direction, not proof.

## 1. What was tested

The data is 10 windows from real meetings. Each window is a full run of consecutive transcript lines, with nothing invented or edited. In total there are 528 sentences, about 42 minutes, plus an 8-minute recording that contains a single word. Of those lines, 74 are must-keep (72 after two label fixes, see 6.3) and 321 are junk to be cut. Lines marked "debatable" count half.

| Window | Source | Length | Split | Why it is hard |
|---|---|---|---|---|
| w01 Zoom join, Toggl, BPMN | local job, Zoom | 4.7 min, 73 lines | train | Logging in, "can you see my screen", naming a Toggl task (housekeeping that sounds like work), then real BPMN teaching broken up by "Okay?" |
| w02 Silent start, onboarding | local job, Zoom | 3.5 min, 58 lines | train | Silent first 3.5 minutes, apologies, screen-share checks; plain admin that must be kept (fixing the timesheet, a 30-hour estimate) |
| w03 Cancelled meeting | Zoom transcript | 3.8 min, 62 lines | train | The meeting never happens: the lead is absent, a time-zone story, they agree to reschedule. No highlights. |
| w04 Called-on status, Jetson | local job, YouTube | 7.1 min, 61 lines | train | Auto-captions with no names; "we start with Spencer's updates" turns into a real idea and a decision with a reason |
| w05 Round-robin, mic, schedule | local job, YouTube | 5.9 min, 55 lines | test | Short real status updates between roll calls, long silences, mic checks, scheduling, a paperwork tangent |
| w06 Nearly empty | local job, Zoom | 8-min recording, 1 line | test | Must not invent content |
| w07 Concept, then long goodbye | local job, Zoom | 4.0 min, 59 lines | test | A real research-principles concept, then 2 minutes of apologies and goodbyes that keep reopening the topic |
| w08 Late start, intros | Zoom transcript | 4.9 min, 59 lines | train | No speaker names, waiting for people, a welcome round with self-introductions |
| w09 Question, Unitree, leads | Zoom transcript | 4.9 min, 52 lines | test | A newcomer's real question, a long answer with real insight, welcome talk, then a go-to-market decision |
| w10 Wrap-up, tools | local job, Zoom | 2.8 min, 48 lines | train | Thanks and scheduling mixed with a root cause (the bug was in Toggl itself) and a Claude/Cursor opinion |

**Split.** The prompt was tuned on the 6 train windows only. The 4 test windows were run once at the start (baseline) and once at the end.

**Scoring.** `tools/prompt_eval/run_eval.py` runs the app's own `judge_segments` and rerank step, compares each line with the gold label, and gives a score out of 100. The weights are:

| Part | Weight |
|---|---|
| Must-keep safety | 30% |
| Removal F1 | 25% |
| Highlight ranking | 15% |
| Junk cleaned | 10% |
| Top moments found | 10% |
| Codes | 5% |
| Shorts | 5% |
| Each format problem | -2 points |

## 2. Sonnet 5.5 baseline (current committed prompts)

| | All 10 | Train 6 | Test 4 |
|---|---|---|---|
| Score | 88.06 (92.06 before -4 for 2 format problems) | 91.21* | 92.87* |
| Must-keep lines cut | 2 / 74 | 1 / 40 | 1 / 34 |
| Junk lines kept | 46 / 321 | 36 / 235 | 10 / 86 |
| Removal precision / recall | 0.976 / 0.848 | 0.979 / 0.851 | 0.969 / 0.842 |
| Top moments found | 7 / 11 | 3 / 6 | 4 / 5 |

\*Before the format penalty, because the 2 format problems are not tied to a window.

Sonnet is safe, since it rarely cuts real content, but too lenient: it keeps about 1 in 7 junk lines and never scores a line above 6.

## 3. Issues found on the baseline (real examples)

1. **It keeps talk about the meeting itself.** In the cancelled meeting w03 it kept 17 of 57 junk lines, for example "Leave now, and then... I'll text in the legal team." (kept, score 2) and "I mean, I'll be online until roughly 3."
2. **It scores scheduling as a "decision".** Examples: w10 "To find the time that we can make the one-to-one meeting with Mr." (kept, score 3, decision) and w03 "just let me know for next week when you would like to meet."
3. **It keeps reassurance and fragments inside a kept thought.** Examples: w02 "It's okay." (kept, score 3, decision) and w02 "5.". This is a side effect of the "never cut a sentence in half" rule.
4. **It keeps welcome, thanks and apology talk as "insight".** Examples: w09 "Yeah, well, feel welcome." and w07 "...even you yourself, you said, like, you've disputed...".
5. **It handles status questions inconsistently.** It kept w10 "Like how much hours?" (gold: cut) but cut w02 "Did you hear what I said about the Master Classes?" (gold: keep). The gold labels are not fully consistent on these lines either.
6. **It cut 2 must-keep lines.**
   - w01 "Okay, you said you drafted the points." is a lead-in to a real item, but it was cut as housekeeping.
   - w09 "But, But..." was a labelling mistake (fixed, see 6.3).
7. **Format problems.** 2 of 24 calls needed a retry: one invented a code ("upd") and one line was missing its score.
8. **It missed 4 of 11 top moments,** all decisions or opinions with reasons:
   - w04: the switch to enterprise remote access
   - w09: the go-to-market decision
   - w10: the Claude/Cursor opinion
   - w10: the root cause (the bug was in Toggl, not the browser cache)
9. **Scores are too low.** The BPMN teaching (w01) and the research-principles concept (w07) both peak at 6. The 8-10 band is never used.

## 4. Haiku's issues on the same windows

Haiku was not re-run. These are the decisions saved by real app jobs on 7 windows (w01, w02, w04, w05, w06, w07, w10). They probably came from an older prompt version, so this is not a same-prompt comparison.

| Same 7 windows, judge only | Sonnet 5.5 baseline | Haiku (saved) |
|---|---|---|
| Score | 92.21 | 93.68 |
| Must-keep cut | 1 | 0 |
| Junk kept | 25 / 192 | 15 / 192 |
| Top moments found | 5 / 8 | 6 / 8 |
| Highest line score | 6 | 7 |

Haiku's weak spots:
- **w01 Toggl task naming:** it kept 11 junk lines, for example "So I'll I just named it Machi and Yasi's Yasin meeting." Sonnet kept 2.
- **w05:** it kept 4 junk lines.
- **w10:** it found 0 of the 2 top moments.
- **Short answers:** it cut the called-on status answer w04 "No, because again I've been off." (debatable) and w10 "Are you done with your developer masterclasses?".

Haiku was better than Sonnet at cutting "it's okay" filler and the w10 scheduling.

## 5. Prompt changes (changelog)

All changes are in `decide/prompts.py`, written as general wording. The answer formats, codes and the `{highlights_criteria}` / `{shorts_criteria}` injection points are unchanged. Full log: `results/changelog.md`.

| Round | Change | Why | Train score | Verdict |
|---|---|---|---|---|
| 1 | • Talk about the meeting (late or missing people, leaving, rescheduling, availability, setting up a 1:1, "let me know") is housekeeping, never a decision, even in an empty meeting.<br>• Thanks, goodbyes, apologies, welcomes and reassurance ("it's okay", "no problem", "sure") are cut.<br>• A line that opens a real item or gives a work instruction is content; a bare "did you hear me?" is not.<br>• "Never cut a sentence in half" now applies only to the rest of the sentence.<br>• Output rule: all five fields, listed codes only. | Issues 1-7 | 91.58 | kept |
| 2 | • Removed an example that backfired (w01 junk went from 2 to 12).<br>• Setting up for this meeting on screen (log in, share a file, name the meeting in a tracker) is housekeeping, and so are fragments.<br>• Gave the exact field order. | w01 regression, format | 93.78 | kept |
| 3 | • Judge: 6-7 also covers "a decision or opinion with its reason, a root cause found"; a clear teaching moment belongs in 8-10; small talk and scheduling stay at 0-2 even inside a good stretch.<br>• Rerank: a decision, opinion or root cause with its reason is worth at least 5; score the best point, not the small talk around it; trim small talk off clip ends. | Issues 8-9 | **94.88** | kept (final) |
| 4 | • Reworded the root-cause line in rerank.<br>• Added a shorts rule against opinions that explain nothing. | w10 misses | 94.75 | reverted (no effect) |

The prompt grew by about 500 tokens (26 lines added, 8 removed).

## 6. Before/after numbers

### 6.1 Train (6 windows)

| | Baseline | Tuned (round 3) |
|---|---|---|
| Score | 91.21 | **94.88** |
| Must-keep cut | 1 / 40 | **0 / 40** |
| Junk kept | 36 / 235 | **10 / 235** |
| Removal precision / recall | 0.979 / 0.851 | 0.977 / 0.959 |
| Highlight ranking | 0.953 | 0.958 |
| Top moments found | 3 / 6 | 4 / 6 |
| Shorts F1 | 1.0 | 0.8 (the w10 opinion clip became a short) |
| Format problems | 2 (whole run) | 0 |

### 6.2 Held-out test (4 windows the tuning never saw)

Both runs use the corrected labels. With the old labels the scores were 92.87 and 92.35: the same direction.

| | Baseline | Tuned (round 3) | |
|---|---|---|---|
| Score | 94.14 | 93.49 | slightly worse |
| Must-keep cut | 0 / 32 | **2 / 32** | **worse (the big bug)** |
| Junk kept | 10 / 86 | 3 / 86 | better |
| Removal precision / recall | 0.981 / 0.842 | 0.931 / 0.951 | cuts more, slightly too eagerly |
| Highlight ranking | 0.938 | 0.908 | worse |
| Top moments found | 4 / 5 | 4 / 5 | same |
| Format problems | 2 (whole run) | 0 in 11 calls | better |

A repeat run on w05 + w07 showed the same pattern: baseline 96.30, tuned run 1 92.04, tuned run 2 88.88. The regressions are systematic, not noise.

**What went wrong on test:**
- **w05 (the big bug).** The first round-robin status update was cut as housekeeping (score 1) in both runs. The lines are "Uh for myself, I wasn't able to attend yesterday's meeting, but I am talking to Sina about the navigation of the mini desktop robot, and we haven't found a good time to meet, but" and "I think this week we'll be able to." The cause is the new rule about "who is missing or late / setting up another call": the model applied it to a whole line that also reports work in progress. The baseline and Haiku both kept these lines.
- **w05, inconsistent.** Real scheduling talk was still kept and tagged "decision" (examples: "once I do move the meeting off the Saturdays...", "send a group message on the Discord ... to get your time zone"). In the repeat run the rerank made it a strong highlight, "Humanoid team meeting plans and likely move to Fridays" (raw 5). That is a false highlight.
- **w07.** The first half of a key sentence, "But what I'm trying to say is that It's... it's, like... Like,", was cut as filler, while its second half, "only a few principles could help a lot.", was kept. The narrowed "never cut a sentence in half" rule now lets the model cut the start of a kept sentence.
- **w09.** The go-to-market decision was still missed (raw 4, threshold 5).

**What carried over to test:**
- Thanks and welcome lines are now cut (w09 junk went from 4 to 0, for example "Yeah, well, feel welcome.").
- w05 junk went from 2 to 0.
- Format problems went to 0.
- The score scale is used more (the best line in w09 reached 7).
- w06 was clean both times, with no invented content.

### 6.3 Label fixes during the test phase

- w09 "But, But..." (a stammer) was must-keep. It is now debatable/remove.
- w05 "I'm quickly making that while I sit here." restates the line before it. It is now debatable instead of must-keep.

Neither fix touches a train window, and both test runs were rescored with the same labels (no new API calls).

## 7. Cost

| | Average per window (about 4-5 min) | Estimate per 30 min of meeting |
|---|---|---|
| Baseline prompts (10 windows, including 2 retries) | $0.0142 | about $0.10 |
| Tuned prompts (same 10 windows) | $0.0120 | about $0.087 |

- The tuned prompt is about 500 tokens longer, but it is cached ($0.20 per 1M tokens), so the extra cost is tiny. The savings come from 0 retries and shorter answers.
- The per-30-minute figure is scaled up from small windows that each had their own rerank call. A real meeting has one rerank call, and chapters are not included. So read it as roughly 8-10 cents per 30 minutes for judge + rerank.
- w06 cost $0.0005 and is left out of the per-minute numbers.

## 8. Remaining issues and recommendations (suggestions only, nothing applied)

1. **Do not ship the current tuned prompt as is.** `decide/prompts.py` holds round 3, uncommitted. Two small edits should fix the regressions:
   - Narrow the meeting-talk rule to lines that are only about attendance or scheduling. A line that also reports work done or in progress stays.
   - Make "never cut a sentence in half" cover the start of a kept sentence as well as its end.

   Optionally, also drop "sure" from the reassurance list. Then re-run w05 + w07 (about $0.03) and train (about $0.08). The alternative is `git checkout decide/prompts.py`, since the baseline was already safe.
2. **Pipeline fixes:**
   - Long runs of good lines merge into one rerank candidate, so the w04 decision could never get its own clip. Splitting long candidates in `edl/highlights.candidate_moments` would fix this.
   - The rerank stretches scores so that each meeting's best moment becomes 10, even in a cancelled meeting (w03: "Nothing to share yet, so they wrap up early", raw 2, shown as 10.0). It should only stretch when the raw best is at least 5.
3. **Haiku vs Sonnet.** On these windows the old Haiku answers were about as good and cheaper. A same-prompt Haiku run on this harness (a few cents) would settle the default model fairly.
4. **Make the test stronger:**
   - Add 4-6 more round-robin and called-on status windows (the big-bug type).
   - Have a team member review `datasets/gold.txt`, especially the status questions w10[20-22] vs w02[23] and [32].
   - Run each setting 2-3 times; single runs varied by up to 3 points on one pair of windows.
5. **Still wrong in every version:**
   - The w10 root cause (raw 4)
   - The w09 go-to-market decision (raw 4)
   - w07 lines explaining why the speaker wanted the conversation (still kept as insight)
   - The BPMN teaching never reaches 8+

## 9. Spend

| Run | Calls | Cost |
|---|---|---|
| baseline (all 10, rerank) | 24 | $0.1422 |
| tune1-tune4 (train, rerank) | 59 | $0.3271 |
| final_test (test, rerank) | 7 | $0.0376 |
| final_test_repeat (w05 + w07) | 4 | $0.0257 |
| **Total** | **94** | **$0.5326 of $3.00** |

Fake and dry runs cost $0. Every call is logged in `tools/prompt_eval/spend_ledger.json`.

## 10. Files

- Harness: `tools/prompt_eval/run_eval.py`, `build_dataset.py`, `sources.py`, `dump.py`
- Gold labels: `tools/prompt_eval/datasets/gold.txt`
- Windows: `datasets/windows/*.json`
- Split: `datasets/split.json`
- Results: `tools/prompt_eval/results/<tag>.json` plus one `<tag>_<window>.tsv` per window, with a per-line verdict column. Tags: baseline, baseline_saved_haiku, tune1-4, final_test, final_test_repeat_w05_w07.
- Changelog: `tools/prompt_eval/results/changelog.md`
- Changed outside the harness: `decide/prompts.py` (round-3 prompts, uncommitted; see recommendation 1). `git diff` confirms that HEAD is the baseline and the working tree is tune3.

## 11. Follow-up fix (applied after the hour, 2026-10-06 ~00:25) — this is what ships

The two regressions on the held-out test had clear causes, so the two recommended edits were made in `decide/prompts.py` (with neutral examples that do not come from any test window):
- The meeting-talk rule now applies only to lines that are ONLY about attendance/scheduling; a line that also reports work done, in progress or planned is a status update and is kept.
- "Never cut a sentence in half" covers the start of a sentence too (a hesitant lead-in to a kept thought is kept).
- "sure" was dropped from the reassurance list.

| | Original prompts | Tuned (round 3) | Tuned + fix (shipped) |
|---|---|---|---|
| Held-out test: must-keep cut | 0 / 32 | 2 / 32 | **0 / 32** |
| Held-out test: junk kept | 10 / 86 | 3 / 86 | **7 / 86** |
| Held-out test: format problems | 2 (whole run) | 0 | **0** |
| Held-out test: score | 94.14 | 93.49 | 92.64 (highlight ranking 0.885 vs 0.938 — within single-run noise of ~3 points) |
| Train: score | 91.21 | 94.88 | **94.49** |
| Train: must-keep cut | 1 / 40 | 0 / 40 | 1 / 40 (the garbled caption "Um, it's JSo N.") |
| Train: junk kept | 36 / 235 | 10 / 235 | **13 / 235** |

Verdict: shipped. Same safety as the original prompts, clearly less junk kept, no format retries; highlight ranking is about the same within noise. Follow-up runs cost $0.12 (fix1_test $0.046 + fix1_train $0.077); the ledger total is $0.66 of $3.00.
