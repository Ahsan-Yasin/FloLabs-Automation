import io
import json
import subprocess

import pytest

import core.proc as proc_module
from ingest.store import store_video
from ingest.validate import AVSyncError, validate_video


def _fake_probe(video_duration, audio_duration):
    payload = {
        "format": {"duration": str(max(video_duration, audio_duration))},
        "streams": [
            {"codec_type": "video", "duration": str(video_duration)},
            {"codec_type": "audio", "duration": str(audio_duration)},
        ],
    }
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")


def test_store_video_rejects_unsupported_extension():
    with pytest.raises(ValueError):
        store_video("clip.avi", io.BytesIO(b"data"))


def test_store_video_writes_file_and_returns_id():
    video_id, path = store_video("clip.mp4", io.BytesIO(b"fake video bytes"))
    assert path.exists()
    assert path.read_bytes() == b"fake video bytes"
    # uploads live inside their own job folder so DELETE removes them
    assert path.name == "source.mp4"
    assert path.parent.name == video_id


class _FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        from pathlib import Path

        Path(self.opts["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"fake video")

    def extract_info(self, url, download=True):
        self.download([url])
        return {"title": "A meeting"}


def test_download_youtube_into_a_job_folder(monkeypatch, tmp_path):
    """With a destination the download lands in the job folder as source.<ext>
    (so DELETE removes it with the folder) and videos/ stays untouched; a
    transcript already sitting there as source.vtt is never mistaken for it."""
    from core.config import get_settings
    from ingest.youtube import download_youtube

    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", type("m", (), {"YoutubeDL": _FakeYDL}))
    job_folder = get_settings().jobs_dir / "yt-job"
    job_folder.mkdir(parents=True)
    (job_folder / "source.vtt").write_text("WEBVTT")
    _, path, _ = download_youtube("https://youtube.com/watch?v=abc123", dest_dir=job_folder)
    assert path == job_folder / "source.mp4" and path.read_bytes() == b"fake video"
    assert list(get_settings().videos_dir.iterdir()) == []


def test_validate_video_passes_when_in_sync(monkeypatch, tmp_path):
    monkeypatch.setattr(proc_module.subprocess, "run", lambda *a, **k: _fake_probe(100.0, 100.2))
    info = validate_video(tmp_path / "clip.mp4")
    assert info.duration == pytest.approx(100.2)


def test_validate_video_raises_on_desync(monkeypatch, tmp_path):
    monkeypatch.setattr(proc_module.subprocess, "run", lambda *a, **k: _fake_probe(100.0, 105.0))
    with pytest.raises(AVSyncError):
        validate_video(tmp_path / "clip.mp4")


def test_validate_video_catches_desync_when_streams_lack_duration_field(monkeypatch, tmp_path):
    """Regression test: many real files (notably .mkv, common for screen/meeting
    recordings) don't set a per-stream "duration" in ffprobe's output at all —
    only duration_ts + time_base. If stream_duration() fell straight through to
    the container's overall duration whenever "duration" is absent, both
    video_duration and audio_duration would collapse to the same container
    value, drift would always be 0.0, and a genuinely desynced file would pass
    validation silently instead of being skipped."""
    payload = {
        "format": {"duration": "105.0"},
        "streams": [
            # video: 100s via duration_ts * time_base (1000 * 1/10 = 100)
            {"codec_type": "video", "duration_ts": 1000, "time_base": "1/10"},
            # audio: 105s via duration_ts * time_base — 5s drift, over threshold
            {"codec_type": "audio", "duration_ts": 105000, "time_base": "1/1000"},
        ],
    }
    monkeypatch.setattr(
        proc_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    with pytest.raises(AVSyncError):
        validate_video(tmp_path / "clip.mkv")


def test_validate_video_uses_tag_duration_when_present(monkeypatch, tmp_path):
    """Matroska streams commonly carry their real duration only in tags.DURATION."""
    payload = {
        "format": {"duration": "100.0"},
        "streams": [
            {"codec_type": "video", "tags": {"DURATION": "00:01:40.000000000"}},
            {"codec_type": "audio", "tags": {"DURATION": "00:01:45.000000000"}},
        ],
    }
    monkeypatch.setattr(
        proc_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    with pytest.raises(AVSyncError):
        validate_video(tmp_path / "clip.mkv")


def test_validate_video_raises_when_no_audio_stream(monkeypatch, tmp_path):
    payload = {"format": {"duration": "10"}, "streams": [{"codec_type": "video", "duration": "10"}]}
    monkeypatch.setattr(
        proc_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    with pytest.raises(AVSyncError):
        validate_video(tmp_path / "clip.mp4")
