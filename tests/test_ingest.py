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


def test_validate_video_raises_when_no_audio_stream(monkeypatch, tmp_path):
    payload = {"format": {"duration": "10"}, "streams": [{"codec_type": "video", "duration": "10"}]}
    monkeypatch.setattr(
        validate_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    with pytest.raises(AVSyncError):
        validate_video(tmp_path / "clip.mp4")
