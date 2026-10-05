import json
import zipfile
from fractions import Fraction
from pathlib import Path

import httpx
import pytest

import outputs as outputs_module
import pipeline as pipeline_module
import slice.pipeline as slice_pipeline
from core.config import get_settings
from core.errors import JobCancelled
from core.models import (
    Chapter,
    JobOptions,
    JobRecord,
    JobStatus,
    RenderManifest,
    RenderPiece,
    Segment,
    SegmentJudgment,
    Word,
)
from decide.chapters import ChapterResult
from decide.gemini_client import RerankResult
from ingest import zoom as zoom_module
from ingest.validate import VideoInfo
from slice.profile import MediaInfo, StreamHeader
from slice.silence import SilenceMap, SilenceParams


def _fake_media(path):
    return MediaInfo(
        path=Path(path), duration=10.0, video_duration=10.0, fps=Fraction(25), width=640, height=360,
        pix_fmt="yuv420p", total_frames=250, has_audio=True, audio_channels=2, audio_sample_rate=48000,
        audio_duration=10.0,
    )


def _manifest_for(edl, kind="cleaned"):
    total = sum(r.end_frame - r.start_frame for r in edl.ranges)
    return RenderManifest(
        kind=kind, fps=edl.fps or "25/1", width=640, height=360, fade_frames=edl.fade_frames,
        expected_frames=total, measured_frames=total, measured_duration_s=total / 25,
        pieces=[
            RenderPiece(src_start_frame=r.start_frame, src_end_frame=r.end_frame,
                        out_start_frame=sum(x.end_frame - x.start_frame for x in edl.ranges[:i]))
            for i, r in enumerate(edl.ranges)
        ],
    )


# the owner's clips at 25 fps: 6.92 s intro, 5.04 s outro
CLIP_FRAMES = {"intro": 173, "outro": 126}


def _header(frames):
    return StreamHeader(frames, frames / 25, "25/1", 640, 360, "yuv420p", "x", frames / 25, None, 48000, 2, "aac",
                        frames / 25)


def _fake_renderers(monkeypatch, calls, cleaned_progress=((1, 1),), fail=None):
    """Replace every ffmpeg-backed renderer used by outputs.render_outputs
    with a fake that writes a small placeholder file. `calls` records
    (renderer, kind); `fail` maps a renderer/kind name to an exception."""
    fail = fail or {}

    def maybe_fail(name):
        if name in fail:
            raise fail[name]

    def fake_track(source, flac, edl, media, work_dir, kind="cleaned", on_progress=None, overlays=None,
                   extra_steps=0):
        calls.append(("track", kind))
        maybe_fail(kind)
        work_dir.mkdir(parents=True, exist_ok=True)
        if kind == "cleaned" and on_progress:
            for c, t in cleaned_progress:
                on_progress(c, t)
        video = work_dir / f"{kind}_video.mp4"
        video.write_bytes(b"v")
        chunk = work_dir / f"{kind}_audio_000.flac"
        chunk.write_bytes(b"a")
        manifest = _manifest_for(edl, kind)
        return slice_pipeline.Track(kind, Fraction(25), video, [chunk], manifest.expected_frames,
                                    manifest.expected_frames * 1920, manifest)

    def fake_assemble(tracks, out_path, work_dir, kind, on_step=None):
        calls.append(("assemble", kind))
        maybe_fail(f"assemble:{kind}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"mp4:" + "+".join(t.kind for t in tracks).encode())
        return _header(sum(t.frames for t in tracks))

    def fake_card(title, media, seconds, work_dir, font, bold_font=None, subtitle="Full meeting"):
        calls.append(("card", title))
        maybe_fail("card")
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "card.mp4").write_bytes(b"c")
        manifest = RenderManifest(kind="card", fps="25/1", width=640, height=360, expected_frames=50)
        return slice_pipeline.Track("card", Fraction(25), work_dir / "card.mp4", [], 50, 96000, manifest)

    def fake_removed(source, flac, removed, media, out_path, work_dir, font, on_progress=None):
        calls.append(("removed", len(removed)))
        maybe_fail("removed")
        shown = [r for r in removed if r.tier == "video"]
        if not shown:
            return None
        out_path.write_bytes(b"removed")
        manifest = RenderManifest(
            kind="removed", fps="25/1", width=640, height=360,
            pieces=[RenderPiece(src_start_frame=r.start_frame, src_end_frame=r.end_frame,
                                out_start_frame=sum(x.end_frame - x.start_frame for x in shown[:i]))
                    for i, r in enumerate(shown)],
            expected_frames=sum(r.end_frame - r.start_frame for r in shown))
        manifest.measured_duration_s = manifest.expected_frames / 25
        return manifest

    def fake_short(source, flac, clip, media, words, out_path, srt_path, work_dir, font, **kw):
        calls.append(("short", clip.index))
        maybe_fail(f"short:{clip.index}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"short")
        srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
        return slice_pipeline.ShortResult(clip, out_path, srt_path, 25, clip.end - clip.start, 1)

    def fake_clip(path, media, work_dir, kind):
        calls.append(("clip", kind, Path(path).name))
        maybe_fail(kind)
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / f"{kind}_video.mp4").write_bytes(b"v")
        frames = CLIP_FRAMES[kind]
        manifest = RenderManifest(kind=kind, fps="25/1", width=640, height=360, expected_frames=frames)
        return slice_pipeline.Track(kind, Fraction(25), work_dir / f"{kind}_video.mp4", [], frames, frames * 1920,
                                    manifest)

    def fake_compatible(paths, headers=None):
        # fail={"concat:intro": ...}: that clip's encoding doesn't match the meeting's
        maybe_fail("concat:" + Path(paths[-1]).stem.split("_")[0])

    monkeypatch.setattr(outputs_module, "extract_audio_flac", lambda *a, **k: calls.append(("flac", None)))
    monkeypatch.setattr(outputs_module, "render_track", fake_track)
    monkeypatch.setattr(outputs_module, "assemble", fake_assemble)
    monkeypatch.setattr(outputs_module, "render_card", fake_card)
    monkeypatch.setattr(outputs_module, "render_clip", fake_clip)
    monkeypatch.setattr(outputs_module, "assert_concat_compatible", fake_compatible)
    monkeypatch.setattr(outputs_module, "render_removed", fake_removed)
    monkeypatch.setattr(outputs_module, "render_short_clip", fake_short)


def _patch_common(monkeypatch, transcribe_calls, decisions_segment_lists, render_calls=None, **render_kw):
    """Mock only the genuinely expensive/external calls (ffprobe, WhisperX,
    Gemini, ffmpeg) — everything else (flag_overlaps, build_segments,
    build_edl, remap_transcript, transcripts, report, zip) runs for real."""
    monkeypatch.setattr(
        pipeline_module,
        "validate_video",
        lambda path: VideoInfo(duration=10.0, video_duration=10.0, audio_duration=10.0),
    )

    def fake_transcribe(path):
        transcribe_calls.append(path)
        return [Word(word="hi", start=0.0, end=1.0, speaker="SPEAKER")]

    monkeypatch.setattr(pipeline_module, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline_module, "probe_media", _fake_media)
    # no audio to measure in these fakes: nobody is ever silent
    monkeypatch.setattr(pipeline_module, "find_silences", lambda *a, **k: SilenceMap(audio_s=10.0))

    def fake_judge_segments(segments, caller=None, highlights_criteria=None, on_progress=None, persist_path=None):
        decisions_segment_lists.append(segments)
        if on_progress:
            on_progress(len(segments), len(segments))
        return _judgments(segments)

    monkeypatch.setattr(pipeline_module, "judge_segments", fake_judge_segments)
    monkeypatch.setattr(pipeline_module, "rerank_moments", lambda cands, judgments, **k: RerankResult(cands, True))
    monkeypatch.setattr(pipeline_module, "generate_chapters",
                        lambda words, duration, caller=None: ChapterResult([], False, ["video too short"]))
    _fake_renderers(monkeypatch, render_calls if render_calls is not None else [], **render_kw)


def _judgments(segments, decision="keep", score=0):
    return [
        SegmentJudgment(index=i, start=seg.start, end=seg.end, speaker=seg.speaker, text=seg.text,
                        decision=decision, highlight_score=score, highlight_category="concept" if score else "none")
        for i, seg in enumerate(segments)
    ]


def _make_job(job_id, **kwargs):
    return JobRecord(job_id=job_id, status=JobStatus.QUEUED, source_path="fake.mp4", **kwargs)


def test_uses_uploaded_transcript_and_skips_whisperx(monkeypatch):
    transcribe_calls = []
    segment_lists = []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)

    fake_words = [Word(word="hello", start=0.0, end=1.0, speaker="Jane")]
    fake_segments = [Segment(speaker="Jane", start=0.0, end=1.0, text="hello")]
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path, **k: (fake_words, fake_segments))

    job = _make_job("job-uploaded-ok", native_transcript_path="transcript.vtt")
    updates = []
    pipeline_module.run_pipeline(job, updates.append)

    assert transcribe_calls == []
    assert job.transcript_source == "uploaded_transcript"
    assert job.status == JobStatus.DONE
    assert segment_lists == [fake_segments]
    assert Path(job.transcript_path).exists()


def test_falls_back_to_whisperx_when_uploaded_transcript_unparsable(monkeypatch):
    transcribe_calls = []
    segment_lists = []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path, **k: None)

    job = _make_job("job-uploaded-bad", native_transcript_path="bad.vtt")
    updates = []
    pipeline_module.run_pipeline(job, updates.append)

    assert len(transcribe_calls) == 1
    assert job.transcript_source == "asr"
    assert job.status == JobStatus.DONE


def test_uses_youtube_captions_and_skips_whisperx(monkeypatch):
    transcribe_calls = []
    segment_lists = []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)

    fake_words = [Word(word="hello", start=0.0, end=1.0, speaker="SPEAKER")]
    fake_segments = [Segment(speaker="SPEAKER", start=0.0, end=1.0, text="hello")]
    monkeypatch.setattr(pipeline_module, "fetch_youtube_transcript", lambda url: (fake_words, fake_segments))

    job = _make_job("job-youtube-ok", source_url="https://youtube.com/watch?v=abc")
    updates = []
    pipeline_module.run_pipeline(job, updates.append)

    assert transcribe_calls == []
    assert job.transcript_source == "youtube_captions"
    assert job.status == JobStatus.DONE


def test_falls_back_to_whisperx_when_no_youtube_captions(monkeypatch):
    transcribe_calls = []
    segment_lists = []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    monkeypatch.setattr(pipeline_module, "fetch_youtube_transcript", lambda url: None)

    job = _make_job("job-youtube-none", source_url="https://youtube.com/watch?v=abc")
    updates = []
    pipeline_module.run_pipeline(job, updates.append)

    assert len(transcribe_calls) == 1
    assert job.transcript_source == "asr"
    assert job.status == JobStatus.DONE


def test_progress_resets_between_stages_and_reports_real_values(monkeypatch):
    transcribe_calls = []
    segment_lists = []
    _patch_common(monkeypatch, transcribe_calls, segment_lists, cleaned_progress=((1, 3), (2, 3), (3, 3)))

    def fake_judge_segments(segments, caller=None, highlights_criteria=None, on_progress=None, persist_path=None):
        if on_progress:
            on_progress(1, 2)
            on_progress(2, 2)
        return _judgments(segments)

    monkeypatch.setattr(pipeline_module, "judge_segments", fake_judge_segments)

    job = _make_job("job-progress")
    snapshots = []
    pipeline_module.run_pipeline(
        job, lambda j: snapshots.append((j.status, j.progress_current, j.progress_total))
    )

    assert (JobStatus.DECIDING, 1, 2) in snapshots
    assert (JobStatus.DECIDING, 2, 2) in snapshots
    assert (JobStatus.SLICING, 1, 3) in snapshots
    assert (JobStatus.SLICING, 3, 3) in snapshots
    # each stage starts from a clean 0/0 rather than carrying over the
    # previous stage's numbers
    assert (JobStatus.BUILDING_EDL, 0, 0) in snapshots
    assert (JobStatus.SLICING, 0, 0) in snapshots

    assert job.status == JobStatus.DONE
    assert job.progress_current == 0
    assert job.progress_total == 0


def test_plain_upload_uses_whisperx_as_before(monkeypatch):
    transcribe_calls = []
    segment_lists = []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)

    job = _make_job("job-plain-upload")
    updates = []
    pipeline_module.run_pipeline(job, updates.append)

    assert len(transcribe_calls) == 1
    assert job.transcript_source == "asr"
    assert job.status == JobStatus.DONE


def test_ffmpeg_failure_puts_stderr_tail_in_job_error(monkeypatch):
    from core.proc import MediaCommandError

    transcribe_calls = []
    segment_lists = []
    error = MediaCommandError(
        "ffmpeg failed (exit 1): ffmpeg -i x.mp4 out.mp4\n--- stderr (tail) ---\nInvalid data found",
        ["ffmpeg"], "Invalid data found", 1,
    )
    _patch_common(monkeypatch, transcribe_calls, segment_lists, fail={"cleaned": error})
    job = _make_job("job-ffmpeg-fail")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.FAILED
    assert "Invalid data found" in job.error
    assert "ffmpeg -i x.mp4" in job.error
    assert job.error_code == "internal"


def _meeting(n=40, length=14.0):
    """n sentences of `length` s over a 10-minute recording."""
    words = [Word(word=f"sentence {i}.", start=i * 15.0, end=i * 15.0 + length, speaker="A") for i in range(n)]
    segments = [Segment(speaker="A", start=w.start, end=w.end, text=w.word) for w in words]
    return words, segments


def _long_media(path):
    media = _fake_media(path)
    return MediaInfo(**{**media.__dict__, "duration": 600.0, "video_duration": 600.0, "total_frames": 15000,
                        "audio_duration": 600.0})


def test_decide_only_stops_before_rendering_then_render_reuses_everything(monkeypatch):
    transcribe_calls, segment_lists, renders = [], [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists, renders)
    monkeypatch.setattr(pipeline_module, "probe_media", _long_media)
    words, segments = _meeting()
    parse_calls = []

    def parse(path, **k):
        parse_calls.append(path)
        return words, segments

    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", parse)

    job = _make_job("job-decide-only", native_transcript_path="t.vtt", options=JobOptions(decide_only=True))
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DECIDED
    assert renders == [] and job.output_video_path is None
    selection = json.loads(Path(job.selection_path).read_text(encoding="utf-8"))
    assert set(selection) == {"moments", "highlights", "shorts"}
    assert Path(job.edl_path).exists() and Path(job.removed_edl_path).exists()

    job.options = job.options.model_copy(update={"decide_only": False})
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE
    assert renders.count(("track", "cleaned")) == 1
    assert parse_calls == [Path("t.vtt")]  # the saved transcript was reused, not parsed again


def test_highlights_and_shorts_are_selected_from_scores(monkeypatch):
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    monkeypatch.setattr(pipeline_module, "probe_media", _long_media)
    words, segments = _meeting()
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path, **k: (words, segments))

    def scored(segments, **k):
        js = _judgments(segments)
        for i in (5, 6, 12, 13, 20, 21, 28, 29, 35, 36):
            js[i] = js[i].model_copy(update={"highlight_score": 9, "highlight_category": "concept"})
        return js

    monkeypatch.setattr(pipeline_module, "judge_segments", scored)

    def rerank(cands, judgments, **k):
        return RerankResult([m.model_copy(update={"short_worthy": True, "title": f"m{m.id}", "reranked": True,
                                                  "score": 9 - m.id}) for m in cands], True)

    monkeypatch.setattr(pipeline_module, "rerank_moments", rerank)
    job = _make_job("job-select", native_transcript_path="t.vtt", options=JobOptions(decide_only=True))
    pipeline_module.run_pipeline(job, lambda j: None)
    selection = json.loads(Path(job.selection_path).read_text(encoding="utf-8"))
    assert len(selection["moments"]) == 5
    assert selection["highlights"]["total_s"] >= 60
    assert (get_settings().jobs_dir / job.job_id / "edl_highlights.json").exists()
    assert 1 <= len(selection["shorts"]) <= 3
    assert all(20 <= s["end"] - s["start"] <= 60 for s in selection["shorts"])


def test_chapters_are_written_when_valid(monkeypatch):
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    monkeypatch.setattr(pipeline_module, "probe_media", _long_media)
    words, segments = _meeting()
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path, **k: (words, segments))
    chapters = [Chapter(start=0.0, title="Intro"), Chapter(start=120.0, title="Plans"),
                Chapter(start=300.0, title="Wrap-up")]
    monkeypatch.setattr(pipeline_module, "generate_chapters",
                        lambda words, duration, caller=None: ChapterResult(chapters, True))
    job = _make_job("job-chapters", native_transcript_path="t.vtt")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE
    # no highlights reel (all scores 0): chapters on the cleaned meeting as is
    assert job.final_offset_s == 0
    assert Path(job.chapters_path).read_text(encoding="utf-8").splitlines() == [
        "00:00 Intro", "02:00 Plans", "05:00 Wrap-up"]


def test_every_removed_range_gets_a_reason():
    from core.models import RemovedRange

    words = [Word(word="Um.", start=10.0, end=11.0, speaker="A"), Word(word="Next.", start=30.0, end=31.0, speaker="A")]
    judgments = [SegmentJudgment(index=0, start=10.0, end=11.0, decision="remove", removal_category="filler"),
                 SegmentJudgment(index=1, start=30.0, end=31.0, decision="keep")]
    removed = [RemovedRange(start=9.5, end=11.5), RemovedRange(start=20.0, end=22.0),
               RemovedRange(start=29.0, end=31.5)]
    pipeline_module.label_removed(removed, judgments, words)
    assert [r.reason for r in removed] == ["filler", "silence (no one speaking)", "trimmed at a cut"]
    assert not any(r.silence for r in removed)  # no audio measured: nothing is flagged as pure silence


def test_a_removed_range_with_speech_in_it_is_never_labelled_silence():
    """A kept sentence (10-20 s) pauses from 12 s into a removed filler
    (20-22 s, spoken from 20.5): the removed range 12.3-22 is mostly pause but
    someone speaks in it, and it goes into removed.mp4 with its label."""
    from core.models import RemovedRange

    words = [Word(word="A kept point.", start=10.0, end=20.0, speaker="A"),
             Word(word="Um okay so.", start=20.0, end=22.0, speaker="A")]
    judgments = [SegmentJudgment(index=0, start=10.0, end=20.0, decision="keep"),
                 SegmentJudgment(index=1, start=20.0, end=22.0, decision="remove", removal_category="filler")]
    pause, merged, gap = (RemovedRange(start=14.0, end=16.0, tier="video"),
                          RemovedRange(start=12.3, end=22.0, tier="video"),
                          RemovedRange(start=30.0, end=33.0, tier="video"))
    pipeline_module.label_removed([pause, merged, gap], judgments, words, silences=[(12.0, 20.5), (29.0, 34.0)])
    assert (merged.reason, merged.silence, merged.tier) == ("filler", False, "video")  # was "silence"
    assert (pause.reason, pause.silence, pause.tier) == ("silence (no one speaking)", True, "transcript_only")
    assert gap.silence and gap.reason == "silence (no one speaking)"
    # a range no removed sentence explains, with a kept sentence's midpoint in its pause: still silence
    trimmed = RemovedRange(start=13.0, end=17.5, tier="video")
    pipeline_module.label_removed([trimmed], judgments, words, silences=[(12.0, 17.0)])
    assert trimmed.reason == "silence (no one speaking)" and not trimmed.silence


def _scored_meeting(monkeypatch, render_calls, **render_kw):
    """10-minute meeting with five funny moments -> a reel and shorts."""
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists, render_calls, **render_kw)
    monkeypatch.setattr(pipeline_module, "probe_media", _long_media)
    words, segments = _meeting()
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path, **k: (words, segments))

    def scored(segments, **k):
        js = _judgments(segments)
        for i in (5, 6, 12, 13, 20, 21, 28, 29, 35, 36):
            js[i] = js[i].model_copy(update={"highlight_score": 9, "highlight_category": "concept"})
        for i in (2, 3, 17):  # three removed sentences -> removed.mp4 + transcript_removed
            js[i] = js[i].model_copy(update={"decision": "remove", "removal_category": "housekeeping"})
        return js

    monkeypatch.setattr(pipeline_module, "judge_segments", scored)
    monkeypatch.setattr(pipeline_module, "rerank_moments", lambda cands, judgments, **k: RerankResult(
        [m.model_copy(update={"short_worthy": True, "title": f"Moment {m.id}", "hook": "why", "reranked": True,
                              "score": 9 - m.id}) for m in cands], True))
    chapters = [Chapter(start=0.0, title="Intro"), Chapter(start=120.0, title="Plans"),
                Chapter(start=300.0, title="Wrap-up")]
    monkeypatch.setattr(pipeline_module, "generate_chapters",
                        lambda words, duration, caller=None: ChapterResult(chapters, True))


def test_done_job_delivers_the_zip_with_manifest_and_all_outputs(monkeypatch):
    calls = []
    _scored_meeting(monkeypatch, calls)
    job = _make_job("job-deliver", native_transcript_path="t.vtt", title="Weekly sync | August 2, 2026")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE, job.error
    job_dir = get_settings().jobs_dir / job.job_id

    # the owner's name: Final_<Meeting>_<date>_Youtube.mp4, the date taken
    # from the title (an upload) and kept on the job for re-renders
    final_name = "Final_WeeklySync_2026-08-02_Youtube.mp4"
    assert job.meeting_date == "2026-08-02"
    assert job.artifacts["final.mp4"].path == final_name and job.output_video_path == str(job_dir / final_name)
    assert not (job_dir / "final.mp4").exists()
    # the final video = highlights + title card + cleaned meeting, in that order
    assert (job_dir / final_name).read_bytes() == b"mp4:highlights+card+cleaned"
    assert ("card", "Weekly sync | August 2, 2026") in calls
    hl_frames = json.loads((job_dir / "edl_highlights.json").read_text(encoding="utf-8"))
    hl_frames = sum(r["end_frame"] - r["start_frame"] for r in hl_frames["ranges"])
    assert job.final_offset_s == (hl_frames + 50) / 25
    # chapters moved behind the reel, with "Highlights" first
    lines = Path(job.chapters_path).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "00:00 Highlights" and lines[1].endswith("Intro") and len(lines) == 4

    mandatory = {"final.mp4", "transcript_clean.txt", "transcript_clean.json", "transcript_removed.txt",
                 "transcript_removed.json", "manifest.json"}
    assert mandatory <= {n for n, a in job.artifacts.items() if a.mandatory and a.status == "ok"}
    for name in ("highlights.mp4", "removed.mp4", "report.pdf", "chapters.txt", "shorts/short_01.mp4",
                 "shorts/short_01.srt", "shorts/shorts.json"):
        assert job.artifacts[name].status == "ok", name
    with zipfile.ZipFile(job.bundle_path) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        assert zf.getinfo(final_name).compress_type == zipfile.ZIP_STORED and "final.mp4" not in names
        assert zf.getinfo("report.pdf").compress_type == zipfile.ZIP_DEFLATED
        assert zf.read("report.pdf").startswith(b"%PDF")
    assert {a.path for a in job.artifacts.values() if a.status == "ok"} == names
    assert manifest["artifacts"]["final.mp4"]["sha256"] == job.artifacts["final.mp4"].sha256
    assert manifest["final"]["cleaned_starts_at_s"] == round(job.final_offset_s, 3)
    assert manifest["final"]["file"] == final_name and manifest["artifacts"]["final.mp4"]["path"] == final_name
    assert manifest["removed"]["cuts"] >= 2
    removed_txt = (job_dir / "transcript_removed.txt").read_text(encoding="utf-8")
    assert "housekeeping" in removed_txt and "sentence 2." in removed_txt
    clean = json.loads((job_dir / "transcript_clean.json").read_text(encoding="utf-8"))
    # final.mp4 times: the reel's lines first (from 0), then the meeting's after the card
    reel_lines = [x for x in clean["lines"] if x["section"] == "highlights"]
    meeting_lines = [x for x in clean["lines"] if x["section"] == "meeting"]
    assert reel_lines and reel_lines[0]["start"] < 1.0 and reel_lines[-1]["end"] <= job.final_offset_s
    assert meeting_lines[0]["start"] >= job.final_offset_s
    clean_txt = (job_dir / "transcript_clean.txt").read_text(encoding="utf-8")
    assert "--- HIGHLIGHTS REEL ---" in clean_txt and f"Times are positions in {final_name} (" in clean_txt
    assert clean["video"] == final_name
    assert not (job_dir / "tmp").exists() and not (job_dir / "audio.flac").exists()


def test_a_zoom_rerender_of_an_old_job_renames_its_final_video(monkeypatch):
    """A job made before the Final_..._Youtube.mp4 names (final.mp4 on disk)
    re-rendered: the new video takes the owner's name and Zoom's date, the
    old final.mp4 goes, the key stays "final.mp4"."""
    from core.models import ArtifactInfo

    calls = []
    _scored_meeting(monkeypatch, calls)
    job = _make_job("job-old-final", native_transcript_path="t.vtt", title="All Tech Team Meeting | August 2, 2026",
                    zoom_meeting={"start_time": "2026-08-03T09:00:00Z"},
                    artifacts={"final.mp4": ArtifactInfo(path="final.mp4", mandatory=True)})
    job_dir = get_settings().jobs_dir / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "final.mp4").write_bytes(b"old render")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE, job.error
    name = "Final_AllTechTeamMeeting_2026-08-03_Youtube.mp4"
    assert job.meeting_date == "2026-08-03" and job.artifacts["final.mp4"].path == name
    assert (job_dir / name).exists() and not (job_dir / "final.mp4").exists()
    with zipfile.ZipFile(job.bundle_path) as zf:
        assert name in zf.namelist() and "final.mp4" not in zf.namelist()
    # a second re-render keeps the stored date and the same name
    job.zoom_meeting = None
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.artifacts["final.mp4"].path == name and (job_dir / name).exists()


def test_an_optional_output_failing_is_only_a_warning(monkeypatch):
    calls = []
    _scored_meeting(monkeypatch, calls, fail={"removed": RuntimeError("drawtext exploded"),
                                              "short:1": RuntimeError("libass missing")})
    job = _make_job("job-partial", native_transcript_path="t.vtt")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE
    assert job.artifacts["removed.mp4"].status == "failed" and "drawtext" in job.artifacts["removed.mp4"].reason
    assert job.artifacts["shorts/short_01.mp4"].status == "failed"
    assert any("removed-parts video failed" in w for w in job.warnings)
    with zipfile.ZipFile(job.bundle_path) as zf:
        assert "removed.mp4" not in zf.namelist()
        assert json.loads(zf.read("manifest.json"))["artifacts"]["removed.mp4"]["status"] == "failed"


def test_a_cancel_during_an_optional_output_still_cancels_the_job(monkeypatch):
    from core.errors import JobCancelled

    _scored_meeting(monkeypatch, [], fail={"removed": JobCancelled("cancelled by DELETE")})
    job = _make_job("job-cancel-optional", native_transcript_path="t.vtt")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.CANCELLED and job.bundle_path is None


def test_a_card_that_does_not_concat_falls_back_to_no_card(monkeypatch):
    from core.errors import RenderAssertError

    calls = []
    _scored_meeting(monkeypatch, calls)
    real_assemble = outputs_module.assemble
    attempts = []

    def picky(tracks, out_path, work_dir, kind, on_step=None):
        attempts.append([t.kind for t in tracks])
        if any(t.kind == "card" for t in tracks):
            raise RenderAssertError("concat piece card_video.mp4 has extradata_hash='a'")
        return real_assemble(tracks, out_path, work_dir, kind, on_step)

    monkeypatch.setattr(outputs_module, "assemble", picky)
    job = _make_job("job-card-fallback", native_transcript_path="t.vtt")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE
    assert attempts[:2] == [["highlights", "card", "cleaned"], ["highlights", "cleaned"]]
    assert any("title card skipped" in w for w in job.warnings)


def _owner_clips():
    folder = get_settings().intro_outro_dir  # a per-test folder (tests/conftest.py)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("CTD - Opening.mp4", "FloLabs - Widescreen outro.mp4"):
        (folder / name).write_bytes(b"x")


def test_intro_and_outro_wrap_final_mp4_and_shift_its_timeline(monkeypatch):
    import math

    from core.timeline import fmt_clock

    calls = []
    _scored_meeting(monkeypatch, calls)
    _owner_clips()
    job = _make_job("job-intro-outro", native_transcript_path="t.vtt", title="Weekly sync")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE, job.error
    job_dir = get_settings().jobs_dir / job.job_id
    assert (job_dir / job.artifacts["final.mp4"].path).read_bytes() == b"mp4:intro+highlights+card+cleaned+outro"
    assert ("clip", "intro", "CTD - Opening.mp4") in calls and ("clip", "outro", "FloLabs - Widescreen outro.mp4") in calls
    # only final.mp4 gets them
    assert (job_dir / "highlights.mp4").read_bytes() == b"mp4:highlights"
    hl = json.loads((job_dir / "edl_highlights.json").read_text(encoding="utf-8"))
    hl_frames = sum(r["end_frame"] - r["start_frame"] for r in hl["ranges"])
    assert job.final_offset_s == (173 + hl_frames + 50) / 25
    assert (job.intro_s, job.outro_s) == (173 / 25, 126 / 25)

    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    final = manifest["final"]
    assert (final["intro_file"], final["outro_file"]) == ("CTD - Opening.mp4", "FloLabs - Widescreen outro.mp4")
    assert (final["intro_s"], final["outro_s"], final["title_card_s"]) == (6.92, 5.04, 2.0)
    parts = final["intro_s"] + final["highlights_s"] + final["title_card_s"] + final["cleaned_s"] + final["outro_s"]
    assert abs(parts - final["duration_s"]) < 0.002 and final["cleaned_starts_at_s"] == round(job.final_offset_s, 3)
    assert manifest["highlights"][0]["final_at_s"] == 6.92  # the reel starts after the intro
    assert job.artifacts["final.mp4"].duration_s == final["duration_s"]

    clean = json.loads((job_dir / "transcript_clean.json").read_text(encoding="utf-8"))
    reel_lines = [x for x in clean["lines"] if x["section"] == "highlights"]
    meeting_lines = [x for x in clean["lines"] if x["section"] == "meeting"]
    assert 6.92 <= reel_lines[0]["start"] < 6.92 + 1.0 and reel_lines[-1]["end"] <= job.final_offset_s
    assert meeting_lines[0]["start"] == round(job.final_offset_s, 3)  # "sentence 0." starts the meeting
    assert clean["intro_s"] == 6.92 and clean["outro_s"] == 5.04
    # "Highlights" covers intro + reel + card; the topics move by all three
    offset = math.floor(job.final_offset_s + 1e-6)
    assert Path(job.chapters_path).read_text(encoding="utf-8").splitlines() == [
        "00:00 Highlights", f"{fmt_clock(offset)} Intro", f"{fmt_clock(offset + 120)} Plans",
        f"{fmt_clock(offset + 300)} Wrap-up"]
    assert not any("intro" in w or "outro" in w for w in job.warnings)


def test_the_job_option_turns_the_intro_and_outro_off(monkeypatch):
    calls = []
    _scored_meeting(monkeypatch, calls)
    _owner_clips()
    job = _make_job("job-no-intro", native_transcript_path="t.vtt", options=JobOptions(intro_outro=False))
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE
    final_path = get_settings().jobs_dir / job.job_id / job.artifacts["final.mp4"].path
    assert final_path.read_bytes() == b"mp4:highlights+card+cleaned"
    assert not any(c[0] == "clip" for c in calls) and job.intro_s == job.outro_s == 0


def test_a_clip_that_fails_or_does_not_match_is_left_out_with_a_warning(monkeypatch):
    from core.errors import RenderAssertError

    calls = []
    _scored_meeting(monkeypatch, calls, fail={"intro": RuntimeError("moov atom not found"),
                                              "concat:outro": RenderAssertError("concat piece outro_video.mp4 has "
                                                                                "extradata_hash='b'")})
    _owner_clips()
    job = _make_job("job-bad-clips", native_transcript_path="t.vtt")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE, job.error
    job_dir = get_settings().jobs_dir / job.job_id
    assert (job_dir / job.artifacts["final.mp4"].path).read_bytes() == b"mp4:highlights+card+cleaned"
    assert "intro skipped: CTD - Opening.mp4: moov atom not found" in job.warnings
    assert any(w.startswith("outro skipped: FloLabs - Widescreen outro.mp4: concat piece") for w in job.warnings)
    assert job.intro_s == 0 and job.final_offset_s == pytest.approx(
        sum(r["end_frame"] - r["start_frame"] for r in json.loads(
            (job_dir / "edl_highlights.json").read_text(encoding="utf-8"))["ranges"]) / 25 + 2)
    assert json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))["final"]["intro_file"] is None


def test_if_the_joined_file_still_fails_the_card_goes_first_then_the_clips(monkeypatch):
    from core.errors import RenderAssertError

    calls = []
    _scored_meeting(monkeypatch, calls)
    _owner_clips()
    real_assemble = outputs_module.assemble
    attempts = []

    def picky(tracks, out_path, work_dir, kind, on_step=None):
        attempts.append([t.kind for t in tracks])
        if kind == "final" and any(t.kind in ("card", "intro") for t in tracks):
            raise RenderAssertError("final output: 1 video frames, expected exactly 2")
        return real_assemble(tracks, out_path, work_dir, kind, on_step)

    monkeypatch.setattr(outputs_module, "assemble", picky)
    job = _make_job("job-retry-order", native_transcript_path="t.vtt")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE
    assert attempts[:3] == [["intro", "highlights", "card", "cleaned", "outro"],
                            ["intro", "highlights", "cleaned", "outro"], ["highlights", "cleaned", "outro"]]
    assert {"title card skipped: it did not match the meeting video's encoding",
            "intro skipped: it did not match the meeting video's encoding"} <= set(job.warnings)
    assert job.intro_s == 0 and job.outro_s == 126 / 25


def test_ec2_mode_keeps_only_the_zip_and_manifest(monkeypatch):
    monkeypatch.setenv("SERVE_INDIVIDUAL_ARTIFACTS", "false")
    get_settings.cache_clear()
    try:
        _scored_meeting(monkeypatch, [])
        job = _make_job("job-ec2", native_transcript_path="t.vtt")
        pipeline_module.run_pipeline(job, lambda j: None)
        job_dir = get_settings().jobs_dir / job.job_id
        assert job.status == JobStatus.DONE
        assert not (job_dir / job.artifacts["final.mp4"].path).exists() and not (job_dir / "shorts").exists()
        assert (job_dir / "manifest.json").exists() and Path(job.bundle_path).exists()
        assert job.artifacts["final.mp4"].on_disk is False and job.artifacts["manifest.json"].on_disk
    finally:
        monkeypatch.delenv("SERVE_INDIVIDUAL_ARTIFACTS")
        get_settings.cache_clear()


def test_chapter_failure_is_only_a_warning(monkeypatch):
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)

    def boom(*a, **k):
        raise RuntimeError("quota gone")

    monkeypatch.setattr(pipeline_module, "generate_chapters", boom)
    job = _make_job("job-chapters-fail")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE
    assert any("chapters skipped" in w for w in job.warnings)


# ---------------------------------------------------------------- Zoom jobs

_ZOOM_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
_ZOOM_VTT = (
    b"WEBVTT\n\n1\n00:00:00.500 --> 00:00:03.000\nJane Doe: Welcome to the robotics sync.\n\n"
    b"2\n00:00:03.500 --> 00:00:08.000\nBob Smith: The new gripper design ships next week.\n\n"
    b"3\n00:00:10.500 --> 00:00:14.000\nJane Doe: This cue starts after the video ends.\n"
)


class _FakeZoomClient:
    """Stands in for ingest.zoom.ZoomClient: a fixed meeting, and downloads
    that write small files (download_meeting itself runs for real)."""

    def __init__(self, meeting):
        self.meeting = meeting
        self.downloads = []

    def get_meeting(self, uuid):
        return self.meeting

    def download_file(self, url, dest, *, size=None, expect_mp4=False, on_bytes=None):
        self.downloads.append(url)
        body = _ZOOM_MP4 if expect_mp4 else _ZOOM_VTT
        dest.write_bytes(body)
        if on_bytes:
            on_bytes(len(body))
        return dest


def _zoom_meeting(with_transcript=True, end="2020-01-06T11:00:00Z"):
    span = {"recording_start": "2020-01-06T10:00:00Z", "recording_end": end, "status": "completed"}
    files = [{"file_type": "MP4", "recording_type": "active_speaker", "download_url": "https://zoom.us/v",
              "file_size": len(_ZOOM_MP4), **span}]
    if with_transcript:
        files.append({"file_type": "TRANSCRIPT", "recording_type": "audio_transcript",
                      "download_url": "https://zoom.us/t", "file_size": len(_ZOOM_VTT), **span})
    return {"uuid": "abc==", "id": 1, "topic": "  Robotics   weekly sync ", "start_time": "2020-01-06T10:00:00Z",
            "duration": 60, "host_email": "host@example.com", "recording_files": files}


def _zoom_client(monkeypatch, meeting):
    client = _FakeZoomClient(meeting)
    monkeypatch.setattr(zoom_module, "get_client", lambda: client)
    return client


def test_zoom_job_downloads_then_uses_zooms_transcript(monkeypatch):
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    client = _zoom_client(monkeypatch, _zoom_meeting())
    job = JobRecord(job_id="job-zoom", zoom_meeting_uuid="abc==")
    statuses = []
    pipeline_module.run_zoom_pipeline(job, lambda j: statuses.append(j.status))

    assert job.status == JobStatus.DONE, job.error
    assert JobStatus.DOWNLOADING in statuses and JobStatus.WAITING_TRANSCRIPT not in statuses
    job_dir = get_settings().jobs_dir / job.job_id
    assert job.source_path == str(job_dir / "source.mp4")
    assert job.native_transcript_path == str(job_dir / "source.vtt")
    assert job.title == "Robotics weekly sync" and job.zoom_meeting["host_email"] == "host@example.com"
    assert job.transcript_source == "zoom_transcript" and transcribe_calls == []
    texts = [s.text for s in segment_lists[0]]
    assert any("gripper" in t for t in texts)
    assert not any("after the video ends" in t for t in texts)  # dropped: past the 10 s media
    assert "downloading" in job.stage_timings
    assert client.downloads == ["https://zoom.us/v", "https://zoom.us/t"]

    # a re-render with the source still on disk downloads nothing ...
    pipeline_module.run_zoom_pipeline(job, lambda j: None)
    assert len(client.downloads) == 2
    # ... and one whose source was deleted after delivery fetches it again
    Path(job.source_path).unlink()
    pipeline_module.run_zoom_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE and len(client.downloads) == 4


def test_zoom_job_without_a_transcript_is_transcribed_here_by_default(monkeypatch):
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    client = _zoom_client(monkeypatch, _zoom_meeting(with_transcript=False))  # years old: no transcript coming
    job = JobRecord(job_id="job-zoom-asr", zoom_meeting_uuid="abc==")
    pipeline_module.run_zoom_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE, job.error
    assert job.native_transcript_path is None and job.transcript_source == "asr"
    assert len(transcribe_calls) == 1 and client.downloads == ["https://zoom.us/v"]


def test_zoom_job_fails_before_downloading_when_this_server_cannot_transcribe(monkeypatch):
    monkeypatch.setenv("REQUIRE_NATIVE_TRANSCRIPT", "true")
    get_settings.cache_clear()
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    client = _zoom_client(monkeypatch, _zoom_meeting(with_transcript=False))
    job = JobRecord(job_id="job-zoom-native", zoom_meeting_uuid="abc==")
    pipeline_module.run_zoom_pipeline(job, lambda j: None)
    assert job.status == JobStatus.FAILED
    assert job.error_code == "transcript_not_ready" and job.retryable is False  # it is never coming
    assert client.downloads == [] and transcribe_calls == []


def test_zoom_job_waits_for_a_pending_transcript_then_fails_retryable(monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setenv("REQUIRE_NATIVE_TRANSCRIPT", "true")
    monkeypatch.setenv("TRANSCRIPT_WAIT_MAX_S", "0")
    get_settings.cache_clear()
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    recent = datetime.now(UTC).isoformat()
    client = _zoom_client(monkeypatch, _zoom_meeting(with_transcript=False, end=recent))
    job = JobRecord(job_id="job-zoom-wait", zoom_meeting_uuid="abc==")
    statuses = []
    pipeline_module.run_zoom_pipeline(job, lambda j: statuses.append(j.status))
    assert JobStatus.WAITING_TRANSCRIPT in statuses
    assert job.status == JobStatus.FAILED and job.error_code == "transcript_not_ready"
    assert job.retryable is True and job.retry_after_s == 300
    assert client.downloads == []


def test_zoom_segment_without_video_is_a_job_warning(monkeypatch):
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    meeting = _zoom_meeting()
    # a later segment whose MP4 was deleted in Zoom: only its audio is left
    meeting["recording_files"].append({"file_type": "M4A", "recording_type": "audio_only", "status": "completed",
                                       "download_url": "https://zoom.us/a2", "recording_start": "2020-01-06T11:10:00Z",
                                       "recording_end": "2020-01-06T11:40:00Z"})
    client = _zoom_client(monkeypatch, meeting)
    job = JobRecord(job_id="job-zoom-gap", zoom_meeting_uuid="abc==")
    pipeline_module.run_zoom_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE, job.error
    assert job.zoom_meeting["segments_without_video"] == 1
    assert any("1 Zoom recording segment" in w for w in job.warnings)
    assert "https://zoom.us/a2" not in client.downloads


class _ChunkedBody(httpx.SyncByteStream):
    """A download that arrives in small chunks and records how far it got."""

    def __init__(self, body: bytes, served: list):
        self.body, self.served = body, served

    def __iter__(self):
        for i in range(0, len(self.body), 1024):
            self.served.append(i)
            yield self.body[i:i + 1024]


def test_cancelling_a_zoom_job_mid_download_leaves_no_parts_behind(monkeypatch):
    for name, value in (("ZOOM_ACCOUNT_ID", "acct"), ("ZOOM_CLIENT_ID", "cid"), ("ZOOM_CLIENT_SECRET", "secret")):
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    body = _ZOOM_MP4 + b"\x00" * 64 * 1024
    meeting = _zoom_meeting()
    meeting["recording_files"][0]["file_size"] = len(body)
    served = []

    def handler(request):
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.url.path.startswith("/v2/meetings/"):
            return httpx.Response(200, json=meeting)
        return httpx.Response(200, stream=_ChunkedBody(body, served))

    client = zoom_module.ZoomClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    monkeypatch.setattr(zoom_module, "get_client", lambda: client)

    def update(job):
        # like the queue's updater after DELETE ?force=true: every update of a
        # running job raises, and the first one mid-download is where it lands
        if job.status == JobStatus.DOWNLOADING and job.progress_total:
            raise JobCancelled("cancelled by DELETE /jobs/{id}?force=true")

    job = JobRecord(job_id="job-zoom-cancel", zoom_meeting_uuid="abc==")
    pipeline_module.run_zoom_pipeline(job, update)

    assert job.status == JobStatus.CANCELLED and job.error_code == "cancelled"
    assert 0 < len(served) < len(body) // 1024  # stopped part-way through the download
    job_dir = get_settings().jobs_dir / job.job_id
    assert not (job_dir / "tmp" / "zoom").exists() and not (job_dir / "source.mp4").exists()
    assert list(job_dir.rglob("*.part")) == [] and segment_lists == []


def test_download_progress_is_throttled_even_when_zoom_omits_file_sizes(monkeypatch):
    monkeypatch.setattr(pipeline_module.time, "monotonic", lambda: 1000.0)  # all within one second
    writes = []
    job = JobRecord(job_id="job-progress")
    on_progress = pipeline_module._download_progress(job, lambda j: writes.append(j.progress_current))
    for done in range(0, 300 * 2**20, 2**20):  # 300 chunks in well under a second, total unknown (0)
        on_progress(done, 0)
    assert len(writes) == 1


def test_upload_without_transcript_fails_when_this_server_cannot_transcribe(monkeypatch):
    monkeypatch.setenv("REQUIRE_NATIVE_TRANSCRIPT", "true")
    get_settings.cache_clear()
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    job = _make_job("job-upload-native")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.FAILED and job.error_code == "transcript_not_ready"
    assert job.retryable is False and transcribe_calls == []


def test_uploaded_transcript_is_clamped_to_the_media_length(monkeypatch):
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    seen = []

    def parse(path, max_duration_s=None):
        seen.append(max_duration_s)
        return [Word(word="hello", start=0.0, end=1.0, speaker="Jane")], [
            Segment(speaker="Jane", start=0.0, end=1.0, text="hello")]

    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", parse)
    job = _make_job("job-upload-clamp", native_transcript_path="t.vtt")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert seen == [10.0] and job.transcript_source == "uploaded_transcript"


# ---------------------------------------------------------------- silence cutting


def _caption_meeting(n=30, length=20.0):
    """A caption transcript: back-to-back sentences whose timings swallow
    every pause (a cue stays on screen until the next one starts)."""
    words = [Word(word=f"sentence {i}.", start=i * length, end=(i + 1) * length, speaker="A") for i in range(n)]
    return words, [Segment(speaker="A", start=w.start, end=w.end, text=w.word) for w in words]


def _silent_meeting(monkeypatch, silences, calls=None, fail=None, note="", **job_kw):
    """A 10-minute caption meeting (sentence 3 removed as housekeeping) whose
    audio has `silences`; returns (job, find_silences calls)."""
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists, calls if calls is not None else [])
    monkeypatch.setattr(pipeline_module, "probe_media", _long_media)
    words, segments = _caption_meeting()
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path, **k: (words, segments))

    def judged(segments, **k):
        js = _judgments(segments)
        js[3] = js[3].model_copy(update={"decision": "remove", "removal_category": "housekeeping"})
        return js

    monkeypatch.setattr(pipeline_module, "judge_segments", judged)
    detect_calls = []

    def fake_find(source, cache_path, params, duration_s, span=None):
        detect_calls.append((cache_path.name, params, span))
        if fail is not None:
            raise fail
        return SilenceMap(ranges=silences, audio_s=600.0, threshold_db=-55.0, floor_db=-74.0, speech_db=-20.0,
                          note=note)

    monkeypatch.setattr(pipeline_module, "find_silences", fake_find)
    job = _make_job("job-silence", native_transcript_path="t.vtt", **job_kw)
    pipeline_module.run_pipeline(job, lambda j: None)
    return job, detect_calls


def test_silence_inside_kept_sentences_is_cut_labelled_and_left_out_of_removed_video(monkeypatch):
    # 47-53: the middle of kept sentence 2 (40-60, its midpoint is in the cut);
    # 130-131.2: a short pause; 598-600: dead air at the very end
    job, detect_calls = _silent_meeting(monkeypatch, [(47.0, 53.0), (130.0, 131.2), (598.0, 600.0)])
    assert job.status == JobStatus.DONE, job.error
    # the threshold is measured over the part the transcript covers
    assert detect_calls == [("silences.json", SilenceParams(), (0.0, 600.0))]
    job_dir = get_settings().jobs_dir / job.job_id
    edl = json.loads((job_dir / "edl.json").read_text(encoding="utf-8"))
    assert edl["silence_cuts"] == [[47.3, 52.75], [130.3, 130.95], [598.3, 600.0]]  # 0.30 s / 0.25 s left in
    kept = [(r["start"], r["end"]) for r in edl["ranges"]]
    assert not any(s < 50.0 < e or s < 130.6 < e or s < 599.0 < e for s, e in kept)

    removed = json.loads((job_dir / "edl_removed.json").read_text(encoding="utf-8"))
    quiet = [r for r in removed if r["silence"]]
    assert [(r["start"], r["end"]) for r in quiet] == [  # the padded cuts, on the 25 fps grid
        pytest.approx((47.3, 52.75), abs=0.041), pytest.approx((130.3, 130.95), abs=0.041),
        pytest.approx((598.3, 600.0), abs=0.041)]
    assert {(r["reason"], r["tier"]) for r in quiet} == {("silence (no one speaking)", "transcript_only")}
    (housekeeping,) = [r for r in removed if not r["silence"]]
    assert housekeeping["tier"] == "video" and "silence" not in housekeeping["reason"]

    cuts = json.loads((job_dir / "transcript_removed.json").read_text(encoding="utf-8"))["cuts"]
    assert [c["in_removed_video"] for c in cuts] == [False, True, False, False]  # only the housekeeping is shown
    assert [c["lines"] for c in cuts if c["silence"]] == [[], [], []]
    assert "(no one speaking)" in (job_dir / "transcript_removed.txt").read_text(encoding="utf-8")
    clean = [x["text"] for x in json.loads((job_dir / "transcript_clean.json").read_text(encoding="utf-8"))["lines"]]
    assert "sentence 2." in clean and "sentence 3." not in clean and len(clean) == 29
    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["removed"]["silence_cuts"] == 3


def test_a_pause_merged_with_a_removed_sentence_keeps_the_kept_text_and_the_removed_label(monkeypatch):
    """Kept sentence 2 (40-60 s) says a few words, then pauses right into
    removed sentence 3 (60-80, housekeeping). The pause cut and the removed
    sentence make ONE removed range, which is not pure silence. It is labelled
    by the removed sentence (it goes into removed.mp4, where someone speaks),
    even though it is more pause (20.2 s) than sentence (20 s); and sentence 2,
    heard in its first 0.8 s, stays in transcript_clean and out of
    transcript_removed (both used to see only pure-silence ranges)."""
    job, _ = _silent_meeting(monkeypatch, [(40.5, 61.0)])
    assert job.status == JobStatus.DONE, job.error
    job_dir = get_settings().jobs_dir / job.job_id
    edl = json.loads((job_dir / "edl.json").read_text(encoding="utf-8"))
    assert edl["silence_cuts"] == [[40.8, 60.0]]  # no padding toward the removed sentence
    removed = json.loads((job_dir / "edl_removed.json").read_text(encoding="utf-8"))
    (merged,) = removed
    assert (merged["start"], merged["end"]) == (pytest.approx(40.8, abs=0.041), 80.0)
    assert (merged["reason"], merged["silence"], merged["tier"]) == ("housekeeping", False, "video")
    (cut,) = json.loads((job_dir / "transcript_removed.json").read_text(encoding="utf-8"))["cuts"]
    assert [line["text"] for line in cut["lines"]] == ["sentence 3."]
    clean = [x["text"] for x in json.loads((job_dir / "transcript_clean.json").read_text(encoding="utf-8"))["lines"]]
    assert "sentence 2." in clean and "sentence 3." not in clean and len(clean) == 29


def test_a_recording_whose_pauses_cannot_be_told_apart_warns_the_owner(monkeypatch):
    note = "room tone (-46 dBFS) is too close to the speech (-40 dBFS typical, -33 loud) ...; nothing is cut"
    job, _ = _silent_meeting(monkeypatch, [], note=note)
    assert job.status == JobStatus.DONE, job.error
    assert f"pauses were not shortened: {note}" in job.warnings


def test_silence_cutting_can_be_switched_off_per_job_or_for_the_server(monkeypatch):
    job, detect_calls = _silent_meeting(monkeypatch, [(47.0, 53.0)], options=JobOptions(cut_silence=False))
    assert job.status == JobStatus.DONE and detect_calls == []
    edl = json.loads(Path(job.edl_path).read_text(encoding="utf-8"))
    assert edl["silence_cuts"] == [] and any(r["start"] < 50.0 < r["end"] for r in edl["ranges"])

    monkeypatch.setenv("SILENCE_CUT_ENABLED", "false")
    get_settings.cache_clear()
    _, detect_calls = _silent_meeting(monkeypatch, [(47.0, 53.0)])
    assert detect_calls == []
    _, detect_calls = _silent_meeting(monkeypatch, [(47.0, 53.0)], options=JobOptions(cut_silence=True))
    assert len(detect_calls) == 1  # the job's own choice wins


def test_a_failed_silence_detection_is_only_a_warning(monkeypatch):
    from core.proc import MediaCommandError

    boom = MediaCommandError("ffmpeg failed (exit 1): ffmpeg -i x\n--- stderr (tail) ---\nno audio", ["ffmpeg"])
    job, _ = _silent_meeting(monkeypatch, [], fail=boom)
    assert job.status == JobStatus.DONE, job.error
    assert any(w.startswith("silence detection failed, pauses were not shortened: ffmpeg failed")
               for w in job.warnings)


def test_a_cancel_during_silence_detection_still_cancels_the_job(monkeypatch):
    job, _ = _silent_meeting(monkeypatch, [], fail=JobCancelled("cancelled"))
    assert job.status == JobStatus.CANCELLED


def test_silence_is_cut_from_the_reel_and_the_edges_of_shorts(monkeypatch):
    _scored_meeting(monkeypatch, [])
    # one pause inside each highlight sentence (5, 12, 20, 28, 35), and one
    # around the start of sentence 5 (75 s), where the first short starts
    silences = [(73.0, 77.0), (80.0, 84.0), (185.0, 189.0), (305.0, 309.0), (425.0, 429.0), (530.0, 534.0)]
    monkeypatch.setattr(pipeline_module, "find_silences", lambda *a, **k: SilenceMap(ranges=silences, audio_s=600.0))
    job = _make_job("job-silent-reel", native_transcript_path="t.vtt", options=JobOptions(decide_only=True))
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DECIDED, job.error
    job_dir = get_settings().jobs_dir / job.job_id
    reel = json.loads((job_dir / "edl_highlights.json").read_text(encoding="utf-8"))
    assert reel["silence_cuts"]
    pauses = [(s + 0.3, e - 0.25) for s, e in silences]
    in_reel = [p for p in pauses if any(r["start"] < p[0] and p[1] < r["end"] for r in reel["ranges"])]
    assert in_reel == []  # none of the pauses inside a clip is left in
    # the second pass uses the normal minimums: pieces are never widened back over a pause
    assert all(r["end_frame"] - r["start_frame"] >= 24 for r in reel["ranges"])
    shorts = json.loads(Path(job.selection_path).read_text(encoding="utf-8"))["shorts"]
    assert any(s["start"] == 76.75 for s in shorts)  # was 75.0: 1.75 s of dead air, now a 0.25 s beat
