"""M4 deliverables: captions, text/filter escaping, transcripts, report, bundle."""

import itertools
import json
import zipfile
from fractions import Fraction
from pathlib import Path

from bundle.package import fill_file_info, sha256_file, write_bundle
from core.models import ArtifactInfo, RemovedRange, RenderManifest, RenderPiece, Word
from report.pdf import ReportData, build_report
from report.text import (
    clean_lines,
    removed_entries,
    write_clean_transcript,
    write_removed_transcript,
)
from slice.captions import Cue, ass_document, caption_cues, srt_text
from slice.ffmpeg_wrapper import build_short_graph, color_params, drawtext
from slice.fonts import filter_escape
from slice.pipeline import card_text, removed_label
from slice.plan import plan_video

# ------------------------------------------------------------------ captions


def test_captions_split_long_sentences_into_short_timed_cues():
    words = [Word(word="So we tried Redis first and it cut latency to a third of what it was.", start=10.0,
                  end=16.0, speaker="A"),
             Word(word="Nice.", start=16.2, end=16.8, speaker="B")]
    cues = caption_cues(words, 10.0, 17.0)
    assert all(len(c.text) <= 30 for c in cues)
    assert " ".join(c.text for c in cues) == words[0].word + " " + words[1].word
    assert cues[0].start == 0.0 and cues[-1].end <= 7.0  # clip timeline
    assert all(a.end <= b.start for a, b in itertools.pairwise(cues))
    assert cues[-1].text == "Nice."  # a sentence end closes a cue


def test_captions_only_keep_words_inside_the_clip():
    words = [Word(word="alpha beta gamma delta", start=0.0, end=4.0, speaker="A")]
    cues = caption_cues(words, 1.0, 3.0)  # beta and gamma have their midpoints inside
    assert [c.text for c in cues] == ["beta gamma"]
    # word times are spread by length: beta starts at 1.043 s -> 0.043 s into the clip
    assert abs(cues[0].start - 0.043) < 0.002 and cues[0].end <= 2.0


def test_overlapping_transcript_lines_never_show_two_captions_at_once():
    """Regression (real footage): rolling caption cues overlap in time; their
    words interleaved and libass stacked two captions on screen."""
    words = [Word(word="So, with OTAA the ESP32.", start=10.0, end=13.0, speaker="A"),
             Word(word="OTAA stands for over the air update.", start=11.5, end=15.0, speaker="A")]
    cues = caption_cues(words, 10.0, 20.0)
    assert all(a.end <= b.start for a, b in itertools.pairwise(cues))
    assert " ".join(c.text for c in cues) == words[0].word + " " + words[1].word  # never interleaved


def test_srt_and_ass_formats():
    cues = [Cue(0.0, 1.5, "hello {there}"), Cue(61.25, 62.0, "back\\slash")]
    srt = srt_text(cues)
    assert "1\n00:00:00,000 --> 00:00:01,500\nhello {there}\n" in srt
    assert "00:01:01,250 --> 00:01:02,000" in srt
    ass = ass_document(cues, title="Coffee {incident}", duration_s=62.0, font_family="Arial", width=1080,
                       height=1920)
    assert "PlayResX: 1080" in ass and "Style: Caption,Arial,76," in ass
    assert "Dialogue: 0,0:00:00.00,0:01:02.00,Title,,0,0,0,,Coffee (incident)" in ass
    assert "0:01:01.25,0:01:02.00,Caption,,0,0,0,,back/slash" in ass  # no override tags from text


# ---------------------------------------------------------------- escaping


def test_disk_full_is_fatal_however_it_surfaces():
    import errno

    from core.errors import classify, is_fatal
    from core.proc import MediaCommandError

    ffmpeg_full = MediaCommandError("ffmpeg failed", ["ffmpeg"], "av_interleaved_write_frame(): No space left on device")
    for exc in (ffmpeg_full, OSError(errno.ENOSPC, "No space left on device")):
        assert is_fatal(exc) and classify(exc) == ("insufficient_disk", True, None)
    assert not is_fatal(MediaCommandError("ffmpeg failed", ["ffmpeg"], "Invalid data found"))


def test_generic_and_storage_file_names_give_no_title():
    from ingest.store import title_from_filename

    assert title_from_filename("3f9a0c1d2b3e4f5a6b7c8d9e0f1a2b3c.mp4") == ""
    assert title_from_filename("source.mp4") == ""
    assert title_from_filename("Weekly_Sync-2026.mp4") == "Weekly Sync 2026"


def test_filter_escape_handles_windows_paths_and_apostrophes():
    # the two levels from the ffmpeg filtergraph docs: ':' -> '\:' -> '\\:', "'" -> "\'" -> "\\\'"
    assert filter_escape("C:/Windows/Fonts/arial.ttf") == "C\\\\:/Windows/Fonts/arial.ttf"
    assert filter_escape("/tmp/O'Brien/x.txt") == "/tmp/O\\\\\\'Brien/x.txt"
    assert filter_escape("a,b;c[d]") == "a\\,b\\;c\\[d\\]"


def test_drawtext_reads_its_text_from_a_file(tmp_path):
    f = drawtext(Path("C:/f.ttf"), tmp_path / "label.txt", size=22)
    assert f.startswith("drawtext=fontfile=") and ":textfile=" in f and ":fontsize=22" in f and "box=1" in f


def test_card_copies_the_source_colour_tags():
    tags = {"color_primaries": "bt709", "color_transfer": "bt709", "color_space": "bt709", "color_range": "tv"}
    assert color_params(tags) == "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,"
    assert color_params({}) == ""


def test_card_text_wraps_and_truncates():
    assert card_text("") == []
    lines = card_text("Quarterly planning for the robotics platform and the new humanoid product line review")
    assert 1 < len(lines) <= 3 and all(len(x) <= 28 for x in lines)


def test_removed_label_uses_source_time_and_reason():
    r = RemovedRange(start=70.4, end=150.9, tier="video", reason="greetings / small talk")
    assert removed_label(r) == "Removed 01:10–02:30 · greetings / small talk"


def test_short_graph_blurs_a_small_copy_and_fades_audio():
    g = build_short_graph(frames=750, fps=Fraction(25), width=1080, height=1920, pre=480, samples=1_440_000,
                          subtitles="subtitles=filename=x.ass")
    assert "crop=270:480,boxblur" in g and "scale=1080:1920,setsar=1[bg]" in g
    assert "overlay=(W-w)/2:(H-h)/2,subtitles=filename=x.ass,format=yuv420p[vout]" in g
    assert "atrim=start_sample=480:end_sample=1440480" in g and "afade=t=out:ss=1435200:ns=4800" in g


def test_plan_records_the_range_of_every_input():
    parts = plan_video([(0, 50), (80, 120), (200, 260)], 0, batch_size=2)
    assert [p.input_ranges for p in parts] == [[0, 1], [2]]
    faded = plan_video([(0, 50), (80, 120), (200, 260)], 12, batch_size=2)
    assert [p.input_ranges for p in faded] == [[0, 1], [1, 2], [2]]  # the seam joins ranges 1 and 2


# -------------------------------------------------------------- transcripts


def _words():
    return [Word(word=f"line {i}.", start=i * 10.0, end=i * 10.0 + 8, speaker="A" if i % 3 else "B")
            for i in range(10)]


def _removed():
    return [RemovedRange(start=9.0, end=31.0, start_frame=225, end_frame=775, tier="video", reason="small talk"),
            RemovedRange(start=59.5, end=60.2, start_frame=1488, end_frame=1505, tier="transcript_only",
                         reason="filler")]


def test_removed_entries_attach_the_cut_words_and_removed_video_position():
    manifest = RenderManifest(kind="removed", fps="25/1", width=64, height=64,
                              pieces=[RenderPiece(src_start_frame=225, src_end_frame=775, out_start_frame=0)])
    entries = removed_entries(_removed(), _words(), manifest)
    assert [line["text"] for line in entries[0].lines] == ["line 1.", "line 2."]
    assert entries[0].removed_video_at == 0.0 and entries[1].removed_video_at is None
    assert entries[1].lines == []  # no sentence midpoint inside a 0.7 s gap


def test_transcript_files(tmp_path):
    manifest = RenderManifest(kind="removed", fps="25/1", width=64, height=64,
                              pieces=[RenderPiece(src_start_frame=225, src_end_frame=775, out_start_frame=0)])
    entries = removed_entries(_removed(), _words(), manifest)
    write_removed_transcript(tmp_path / "r.txt", tmp_path / "r.json", entries, meeting="Sync", source_duration_s=100)
    text = (tmp_path / "r.txt").read_text(encoding="utf-8")
    assert "Removed from: Sync" in text and "[00:09–00:31] (22.0s) small talk  [in removed.mp4 at 00:00]" in text
    assert "    A: line 1." in text and "(no speech)" in text and "1 cuts of 1 s or more" in text
    data = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert data["timeline"] == "source" and data["cuts"][0]["in_removed_video"] is True

    # removed.mp4 failed or was skipped: nothing may claim to be in it
    no_video = removed_entries(_removed(), _words(), None)
    write_removed_transcript(tmp_path / "r.txt", tmp_path / "r.json", no_video, meeting="Sync", source_duration_s=100)
    assert "There is no removed.mp4" in (tmp_path / "r.txt").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["cuts"][0]["in_removed_video"] is False

    lines = clean_lines([Word(word="hi", start=0.0, end=1.0, speaker="A"),
                         Word(word="there", start=1.0, end=2.0, speaker="A"),
                         Word(word="yo", start=2.0, end=3.0, speaker="B")], offset_s=62.0)
    write_clean_transcript(tmp_path / "c.txt", tmp_path / "c.json", lines, meeting="Sync", offset_s=62.0,
                           final_duration_s=300)
    text = (tmp_path / "c.txt").read_text(encoding="utf-8")
    assert "[01:02] A: hi there" in text and "[01:04] B: yo" in text
    assert "the full meeting starts at 01:02" in text


def test_clean_transcript_with_an_intro_and_outro(tmp_path):
    reel = [Word(word="the best bit", start=1.0, end=3.0, speaker="A")]
    meeting = [Word(word="hi", start=0.0, end=1.0, speaker="B")]
    # final.mp4: intro 6.92 s, reel from 6.92 s, card, meeting from 70 s
    lines = clean_lines(meeting, offset_s=70.0, reel_words=reel, reel_offset_s=6.92)
    assert [(x["section"], x["start"]) for x in lines] == [("highlights", 7.92), ("meeting", 70.0)]
    write_clean_transcript(tmp_path / "c.txt", tmp_path / "c.json", lines, meeting="Sync", offset_s=70.0,
                           final_duration_s=400, intro_s=6.92, outro_s=5.02)
    text = (tmp_path / "c.txt").read_text(encoding="utf-8")
    assert "final.mp4 opens with the intro (7s) and the highlights reel; the full meeting starts at 01:10." in text
    assert "It ends with the outro (5s) after the meeting." in text and "[00:07] A: the best bit" in text
    data = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert (data["cleaned_starts_at_s"], data["intro_s"], data["outro_s"]) == (70.0, 6.92, 5.02)
    # an intro but no reel
    write_clean_transcript(tmp_path / "c.txt", tmp_path / "c.json", clean_lines(meeting, 6.92), meeting="Sync",
                           offset_s=6.92, final_duration_s=400, intro_s=6.92)
    text = (tmp_path / "c.txt").read_text(encoding="utf-8")
    assert "opens with the intro (7s); the full meeting starts at 00:06." in text and "outro" not in text


def test_reel_rows_start_after_the_intro():
    from core.models import Moment
    from deliver import _reel_rows

    reel = [Moment(id=0, start=10.0, end=20.0, first_index=0, last_index=1, score=9, title="Cache")]
    manifest = RenderManifest(kind="highlights", fps="25/1", width=64, height=64,
                              pieces=[RenderPiece(src_start_frame=250, src_end_frame=500, out_start_frame=0)])
    assert _reel_rows(manifest, reel, offset_s=6.92) == [(6.92, 10.0, 20.0, "Cache")]


# ------------------------------------------------------------------- report


def test_report_pdf_contains_the_removed_parts_and_highlights(tmp_path):
    from pypdf import PdfReader

    from slice.fonts import find_font

    data = ReportData(
        meeting="Weekly sync", job_id="abc123", version="2.0.0", created="2026-09-25 10:00 UTC",
        source_name="source.mp4", source_duration_s=600, final_duration_s=532, cleaned_duration_s=420,
        highlights_duration_s=98, card_s=2, removed=removed_entries(_removed(), _words()),
        intro_name="CTD - Opening.mp4", intro_s=6.93, outro_name="FloLabs - Widescreen outro.mp4", outro_s=5.03,
        merged_back_count=3, merged_back_s=1.2,
        highlights=[(0.0, 120.0, 150.0, "The coffee machine incident")],
        shorts=[("shorts/short_01.mp4", 120.0, 150.0, "Coffee", "It exploded — twice")],
        chapters="00:00 Highlights\n01:40 Intro\n03:00 Plans\n",
        artifacts=[("final.mp4", "ok", 520.0, 10_000_000, ""), ("removed.mp4", "failed", None, None, "boom")],
        warnings=["found 1 short-worthy moment(s), 4 requested"],
        llm_usage={"calls": 25, "prompt_tokens": 100000, "output_tokens": 15000},
        stage_timings={"deciding": 300.0},
    )
    out = tmp_path / "report.pdf"
    build_report(out, data, font=find_font(), bold_font=find_font(bold=True))
    text = "".join(page.extract_text() for page in PdfReader(out).pages)
    for needle in ("Meeting edit report", "Weekly sync", "small talk", "line 1.", "The coffee machine incident",
                   "It exploded", "01:40 Intro", "failed: boom", "100,000 input", "CTD - Opening.mp4 (6.9s)",
                   "FloLabs - Widescreen outro.mp4 (5.0s)"):
        assert needle in text, needle
    # what final.mp4 is made of, in order (the table cell may wrap)
    assert ("= intro 6.9s + highlights 01:38 + title card 2s + cleaned meeting 07:00 + outro 5.0s"
            in " ".join(text.split()))


# ------------------------------------------------------------------- bundle


def test_bundle_stores_video_deflates_text_and_marks_missing_files(tmp_path):
    (tmp_path / "shorts").mkdir()
    (tmp_path / "final.mp4").write_bytes(b"\x00" * 5000)
    (tmp_path / "shorts" / "short_01.mp4").write_bytes(b"s")
    (tmp_path / "transcript_clean.txt").write_text("hello " * 500, encoding="utf-8")
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    artifacts = {
        "final.mp4": ArtifactInfo(path="final.mp4", mandatory=True),
        "shorts/short_01.mp4": ArtifactInfo(path="shorts/short_01.mp4"),
        "transcript_clean.txt": ArtifactInfo(path="transcript_clean.txt", kind="text"),
        "report.pdf": ArtifactInfo(path="report.pdf", kind="pdf"),  # never written
        "removed.mp4": ArtifactInfo(path="removed.mp4", status="failed", reason="x"),
    }
    fill_file_info(tmp_path, artifacts)
    assert artifacts["final.mp4"].bytes == 5000
    assert artifacts["final.mp4"].sha256 == sha256_file(tmp_path / "final.mp4")
    assert artifacts["report.pdf"].status == "failed"
    zip_path = write_bundle(tmp_path, artifacts, tmp_path / "bundle.zip", extra=["manifest.json"])
    with zipfile.ZipFile(zip_path) as zf:
        assert sorted(zf.namelist()) == ["final.mp4", "manifest.json", "shorts/short_01.mp4",
                                         "transcript_clean.txt"]
        assert zf.getinfo("final.mp4").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("transcript_clean.txt").compress_type == zipfile.ZIP_DEFLATED
    assert not (tmp_path / "bundle.zip.tmp").exists()


def test_silences_are_listed_but_not_claimed_for_removed_mp4(tmp_path):
    removed = _removed() + [RemovedRange(start=70.0, end=73.5, start_frame=1750, end_frame=1838,
                                         tier="transcript_only", reason="silence (no one speaking)", silence=True)]
    manifest = RenderManifest(kind="removed", fps="25/1", width=64, height=64,
                              pieces=[RenderPiece(src_start_frame=225, src_end_frame=775, out_start_frame=0)])
    entries = removed_entries(removed, _words(), manifest)
    write_removed_transcript(tmp_path / "r.txt", tmp_path / "r.json", entries, meeting="Sync", source_duration_s=100)
    text = (tmp_path / "r.txt").read_text(encoding="utf-8")
    assert "[01:10–01:13] (3.5s) silence (no one speaking)\n    (no one speaking)" in text
    assert "1 of the cuts (4s) are pauses where no one was speaking" in text
    data = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert data["silence_cuts"] == 1 and data["silence_s"] == 3.5
    assert data["cuts"][2]["silence"] is True and data["cuts"][2]["in_removed_video"] is False


def test_reel_pieces_split_by_cut_pauses_are_one_highlight_row():
    from core.models import Moment
    from deliver import _reel_rows

    reel = [Moment(id=0, start=10.0, end=40.0, first_index=0, last_index=3, score=9, category="concept",
                   title="How the cache works"),
            Moment(id=1, start=100.0, end=130.0, first_index=9, last_index=12, score=8, category="insight")]
    manifest = RenderManifest(kind="highlights", fps="25/1", width=64, height=64, pieces=[
        RenderPiece(src_start_frame=250, src_end_frame=500, out_start_frame=0),  # 10-20 s
        RenderPiece(src_start_frame=550, src_end_frame=1000, out_start_frame=250),  # 22-40 s: a pause was cut
        RenderPiece(src_start_frame=2500, src_end_frame=3250, out_start_frame=700),  # 100-130 s
    ])
    assert _reel_rows(manifest, reel) == [(0.0, 10.0, 40.0, "How the cache works"), (28.0, 100.0, 130.0, "insight")]
