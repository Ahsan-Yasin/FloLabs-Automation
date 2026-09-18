from pathlib import Path

import pipeline as pipeline_module
from core.models import JobRecord, JobStatus, Segment, Word
from ingest.validate import VideoInfo


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

    def fake_get_decisions(segments, mode="crosstalk"):
        decisions_segment_lists.append(segments)
        return []

    monkeypatch.setattr(pipeline_module, "get_decisions", fake_get_decisions)
    monkeypatch.setattr(pipeline_module, "render_output", lambda source, edl, out_path, work_dir: out_path)


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
