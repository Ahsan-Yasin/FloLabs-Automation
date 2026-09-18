import io
import json
import subprocess

import pytest

from ingest import validate as validate_module
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
    assert path.name == f"{video_id}.mp4"


def test_validate_video_passes_when_in_sync(monkeypatch, tmp_path):
    monkeypatch.setattr(validate_module.subprocess, "run", lambda *a, **k: _fake_probe(100.0, 100.2))
    info = validate_video(tmp_path / "clip.mp4")
    assert info.duration == pytest.approx(100.2)


def test_validate_video_raises_on_desync(monkeypatch, tmp_path):
    monkeypatch.setattr(validate_module.subprocess, "run", lambda *a, **k: _fake_probe(100.0, 105.0))
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
        validate_module.subprocess,
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
        validate_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    with pytest.raises(AVSyncError):
        validate_video(tmp_path / "clip.mkv")


def test_validate_video_raises_when_no_audio_stream(monkeypatch, tmp_path):
    payload = {"format": {"duration": "10"}, "streams": [{"codec_type": "video", "duration": "10"}]}
    monkeypatch.setattr(
        validate_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    with pytest.raises(AVSyncError):
        validate_video(tmp_path / "clip.mp4")
