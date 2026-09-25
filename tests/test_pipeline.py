import json
import zipfile
from fractions import Fraction
from pathlib import Path

import outputs as outputs_module
import pipeline as pipeline_module
import slice.pipeline as slice_pipeline
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
from slice.profile import MediaInfo, StreamHeader


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

    monkeypatch.setattr(outputs_module, "extract_audio_flac", lambda *a, **k: calls.append(("flac", None)))
    monkeypatch.setattr(outputs_module, "render_track", fake_track)
    monkeypatch.setattr(outputs_module, "assemble", fake_assemble)
    monkeypatch.setattr(outputs_module, "render_card", fake_card)
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

    def parse(path):
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
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path: (words, segments))

    def scored(segments, **k):
        js = _judgments(segments)
        for i in (5, 6, 12, 13, 20, 21, 28, 29, 35, 36):
            js[i] = js[i].model_copy(update={"highlight_score": 9, "highlight_category": "funny"})
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
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path: (words, segments))
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
    assert [r.reason for r in removed] == ["filler", "pause (no speech)", "trimmed at a cut"]


def _scored_meeting(monkeypatch, render_calls, **render_kw):
    """10-minute meeting with five funny moments -> a reel and shorts."""
    transcribe_calls, segment_lists = [], []
    _patch_common(monkeypatch, transcribe_calls, segment_lists, render_calls, **render_kw)
    monkeypatch.setattr(pipeline_module, "probe_media", _long_media)
    words, segments = _meeting()
    monkeypatch.setattr(pipeline_module, "load_uploaded_transcript", lambda path: (words, segments))

    def scored(segments, **k):
        js = _judgments(segments)
        for i in (5, 6, 12, 13, 20, 21, 28, 29, 35, 36):
            js[i] = js[i].model_copy(update={"highlight_score": 9, "highlight_category": "funny"})
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
    job = _make_job("job-deliver", native_transcript_path="t.vtt", title="Weekly sync")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE, job.error
    job_dir = get_settings().jobs_dir / job.job_id

    # final.mp4 = highlights + title card + cleaned meeting, in that order
    assert (job_dir / "final.mp4").read_bytes() == b"mp4:highlights+card+cleaned"
    assert ("card", "Weekly sync") in calls
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
        assert zf.getinfo("final.mp4").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("report.pdf").compress_type == zipfile.ZIP_DEFLATED
        assert zf.read("report.pdf").startswith(b"%PDF")
    assert {n for n, a in job.artifacts.items() if a.status == "ok"} == names
    assert manifest["artifacts"]["final.mp4"]["sha256"] == job.artifacts["final.mp4"].sha256
    assert manifest["final"]["cleaned_starts_at_s"] == round(job.final_offset_s, 3)
    assert manifest["removed"]["cuts"] >= 2
    removed_txt = (job_dir / "transcript_removed.txt").read_text(encoding="utf-8")
    assert "housekeeping" in removed_txt and "sentence 2." in removed_txt
    clean = json.loads((job_dir / "transcript_clean.json").read_text(encoding="utf-8"))
    # final.mp4 times: the reel's lines first (from 0), then the meeting's after the card
    reel_lines = [x for x in clean["lines"] if x["section"] == "highlights"]
    meeting_lines = [x for x in clean["lines"] if x["section"] == "meeting"]
    assert reel_lines and reel_lines[0]["start"] < 1.0 and reel_lines[-1]["end"] <= job.final_offset_s
    assert meeting_lines[0]["start"] >= job.final_offset_s
    assert "--- HIGHLIGHTS REEL ---" in (job_dir / "transcript_clean.txt").read_text(encoding="utf-8")
    assert not (job_dir / "tmp").exists() and not (job_dir / "audio.flac").exists()


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


def test_ec2_mode_keeps_only_the_zip_and_manifest(monkeypatch):
    monkeypatch.setenv("SERVE_INDIVIDUAL_ARTIFACTS", "false")
    get_settings.cache_clear()
    try:
        _scored_meeting(monkeypatch, [])
        job = _make_job("job-ec2", native_transcript_path="t.vtt")
        pipeline_module.run_pipeline(job, lambda j: None)
        job_dir = get_settings().jobs_dir / job.job_id
        assert job.status == JobStatus.DONE
        assert not (job_dir / "final.mp4").exists() and not (job_dir / "shorts").exists()
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
