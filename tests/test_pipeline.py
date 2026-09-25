import json
from fractions import Fraction
from pathlib import Path

import pipeline as pipeline_module
from core.config import get_settings
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
from ingest.validate import VideoInfo
from slice.profile import MediaInfo


def _fake_media(path):
    return MediaInfo(
        path=Path(path), duration=10.0, video_duration=10.0, fps=Fraction(25), width=640, height=360,
        pix_fmt="yuv420p", total_frames=250, has_audio=True, audio_channels=2, audio_sample_rate=48000,
        audio_duration=10.0,
    )


def _manifest_for(edl):
    total = sum(r.end_frame - r.start_frame for r in edl.ranges)
    return RenderManifest(
        kind="cleaned", fps=edl.fps or "25/1", width=640, height=360, fade_frames=edl.fade_frames,
        expected_frames=total, measured_frames=total, measured_duration_s=total / 25,
        pieces=[
            RenderPiece(src_start_frame=r.start_frame, src_end_frame=r.end_frame,
                        out_start_frame=sum(x.end_frame - x.start_frame for x in edl.ranges[:i]))
            for i, r in enumerate(edl.ranges)
        ],
    )


def _patch_common(monkeypatch, transcribe_calls, decisions_segment_lists):
    """Mock only the genuinely expensive/external calls (ffprobe, WhisperX,
    Gemini, ffmpeg) — everything else (flag_overlaps, build_segments,
    build_edl, remap_transcript) runs for real."""
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
    monkeypatch.setattr(pipeline_module, "extract_audio_flac", lambda *a, **k: None)

    def fake_judge_segments(segments, caller=None, highlights_criteria=None, on_progress=None, persist_path=None):
        decisions_segment_lists.append(segments)
        if on_progress:
            on_progress(len(segments), len(segments))
        return _judgments(segments)

    monkeypatch.setattr(pipeline_module, "judge_segments", fake_judge_segments)
    monkeypatch.setattr(pipeline_module, "rerank_moments", lambda cands, judgments, **k: RerankResult(cands, True))
    monkeypatch.setattr(pipeline_module, "generate_chapters",
                        lambda words, duration, caller=None: ChapterResult([], False, ["video too short"]))

    def fake_render_cleaned(source, audio_flac, edl, media, out_path, work_dir, on_progress=None):
        if on_progress:
            on_progress(len(edl.ranges), len(edl.ranges))
        return _manifest_for(edl)

    monkeypatch.setattr(pipeline_module, "render_cleaned", fake_render_cleaned)


def _judgments(segments, decision="keep", score=0):
    return [
        SegmentJudgment(index=i, start=seg.start, end=seg.end, speaker=seg.speaker, text=seg.text,
                        decision=decision, highlight_score=score, highlight_category="funny" if score else "none")
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
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path: (fake_words, fake_segments))

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
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path: None)

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
    _patch_common(monkeypatch, transcribe_calls, segment_lists)

    def fake_judge_segments(segments, caller=None, highlights_criteria=None, on_progress=None, persist_path=None):
        if on_progress:
            on_progress(1, 2)
            on_progress(2, 2)
        return _judgments(segments)

    monkeypatch.setattr(pipeline_module, "judge_segments", fake_judge_segments)

    def fake_render_cleaned(source, audio_flac, edl, media, out_path, work_dir, on_progress=None):
        if on_progress:
            on_progress(1, 3)
            on_progress(2, 3)
            on_progress(3, 3)
        return _manifest_for(edl)

    monkeypatch.setattr(pipeline_module, "render_cleaned", fake_render_cleaned)

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
    _patch_common(monkeypatch, transcribe_calls, segment_lists)

    def failing_render(source, audio_flac, edl, media, out_path, work_dir, on_progress=None):
        raise MediaCommandError(
            "ffmpeg failed (exit 1): ffmpeg -i x.mp4 out.mp4\n--- stderr (tail) ---\nInvalid data found",
            ["ffmpeg"], "Invalid data found", 1,
        )

    monkeypatch.setattr(pipeline_module, "render_cleaned", failing_render)
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
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    monkeypatch.setattr(pipeline_module, "probe_media", _long_media)
    words, segments = _meeting()
    parse_calls = []

    def parse(path):
        parse_calls.append(path)
        return words, segments

    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", parse)
    renders = []
    fake_render = pipeline_module.render_cleaned

    def counting_render(*a, **k):
        renders.append(1)
        return fake_render(*a, **k)

    monkeypatch.setattr(pipeline_module, "render_cleaned", counting_render)

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
    assert renders == [1]
    assert parse_calls == [Path("t.vtt")]  # the saved transcript was reused, not parsed again


def test_highlights_and_shorts_are_selected_from_scores(monkeypatch):
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    monkeypatch.setattr(pipeline_module, "probe_media", _long_media)
    words, segments = _meeting()
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path: (words, segments))

    def scored(segments, **k):
        js = _judgments(segments)
        for i in (5, 6, 20, 21, 30):
            js[i] = js[i].model_copy(update={"highlight_score": 9, "highlight_category": "funny"})
        return js

    monkeypatch.setattr(pipeline_module, "judge_segments", scored)

    def rerank(cands, judgments, **k):
        return RerankResult([m.model_copy(update={"short_worthy": True, "title": f"m{m.id}"}) for m in cands], True)

    monkeypatch.setattr(pipeline_module, "rerank_moments", rerank)
    job = _make_job("job-select", native_transcript_path="t.vtt", options=JobOptions(decide_only=True))
    pipeline_module.run_pipeline(job, lambda j: None)
    selection = json.loads(Path(job.selection_path).read_text(encoding="utf-8"))
    assert len(selection["moments"]) == 3
    assert selection["highlights"]["total_s"] >= 60
    assert (get_settings().jobs_dir / job.job_id / "edl_highlights.json").exists()
    assert 1 <= len(selection["shorts"]) <= 3
    assert all(20 <= s["end"] - s["start"] <= 60 for s in selection["shorts"])


def test_chapters_are_written_when_valid(monkeypatch):
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists)
    monkeypatch.setattr(pipeline_module, "probe_media", _long_media)
    words, segments = _meeting()
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path: (words, segments))
    chapters = [Chapter(start=0.0, title="Intro"), Chapter(start=120.0, title="Plans"),
                Chapter(start=300.0, title="Wrap-up")]
    monkeypatch.setattr(pipeline_module, "generate_chapters",
                        lambda words, duration, caller=None: ChapterResult(chapters, True))
    job = _make_job("job-chapters", native_transcript_path="t.vtt")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE
    assert Path(job.chapters_path).read_text(encoding="utf-8").splitlines() == [
        "00:00 Intro", "02:00 Plans", "05:00 Wrap-up"]


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
