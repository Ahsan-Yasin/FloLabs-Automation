import logging
from types import SimpleNamespace

import pytest

from ingest import youtube
from ingest.youtube import YoutubeDownloadError, download_youtube


class _FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        # simulate yt-dlp writing the merged file next to outtmpl
        from pathlib import Path

        out = Path(self.opts["outtmpl"].replace("%(ext)s", "mp4"))
        out.write_bytes(b"fake video")

    def extract_info(self, url, download=True):
        self.download([url])
        return {"title": "  Weekly   Tech Sync \n"}


class _FailingYDL(_FakeYDL):
    def download(self, urls):
        raise RuntimeError("network error")


def test_download_youtube_returns_id_path_and_title(monkeypatch):
    fake_module = type("m", (), {"YoutubeDL": _FakeYDL})
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", fake_module)

    video_id, path, title = download_youtube("https://youtube.com/watch?v=abc123")
    assert path.exists()
    assert path.name == f"{video_id}.mp4"
    assert title == "Weekly Tech Sync"


def test_download_youtube_prefers_h264_at_most_720p(monkeypatch):
    seen = []

    class _RecordingYDL(_FakeYDL):
        def __init__(self, opts):
            super().__init__(opts)
            seen.append(opts)

    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", type("m", (), {"YoutubeDL": _RecordingYDL}))
    download_youtube("https://youtube.com/watch?v=abc123")
    first_choice = seen[0]["format"].split("/")[0]
    assert "height<=720" in first_choice and "vcodec^=avc1" in first_choice
    assert seen[0]["format"].endswith("/best")  # never refuse a video that has no such format


def test_download_youtube_wraps_failures(monkeypatch):
    fake_module = type("m", (), {"YoutubeDL": _FailingYDL})
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", fake_module)

    with pytest.raises(YoutubeDownloadError):
        download_youtube("https://youtube.com/watch?v=abc123")


def _which(*found):
    return lambda name: f"/usr/bin/{name}" if name in found else None


def test_base_opts_enable_every_installed_js_runtime(monkeypatch):
    monkeypatch.setattr(youtube.shutil, "which", _which("node"))
    assert youtube.ydl_base_opts()["js_runtimes"] == {"node": {}}

    monkeypatch.setattr(youtube.shutil, "which", _which("deno", "node"))
    assert youtube.ydl_base_opts()["js_runtimes"] == {"deno": {}, "node": {}}


def test_base_opts_leave_yt_dlp_default_when_no_runtime_is_installed(monkeypatch):
    # an empty dict would disable JS runtimes outright rather than keep deno
    monkeypatch.setattr(youtube.shutil, "which", _which())
    assert "js_runtimes" not in youtube.ydl_base_opts()


def test_base_opts_pin_ffmpeg_only_for_a_real_file(monkeypatch, tmp_path):
    # Stubbed rather than set via FFMPEG_BIN: real settings put a real-file
    # FFMPEG_BIN's folder on os.environ["PATH"], and this fake ffmpeg.exe
    # would then shadow the real one for every later test.
    def use_ffmpeg(path):
        monkeypatch.setattr(youtube, "get_settings", lambda: SimpleNamespace(ffmpeg_bin=path))

    use_ffmpeg("ffmpeg")  # a bare name: yt-dlp searches PATH itself
    assert "ffmpeg_location" not in youtube.ydl_base_opts()

    ffmpeg = tmp_path / "bin" / "ffmpeg.exe"
    ffmpeg.parent.mkdir()
    ffmpeg.write_bytes(b"")
    use_ffmpeg(str(ffmpeg))
    assert youtube.ydl_base_opts()["ffmpeg_location"] == str(ffmpeg.parent)


def test_download_youtube_uses_the_base_opts(monkeypatch):
    seen = []

    class _RecordingYDL(_FakeYDL):
        def __init__(self, opts):
            super().__init__(opts)
            seen.append(opts)

    monkeypatch.setattr(youtube.shutil, "which", _which("node"))
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", type("m", (), {"YoutubeDL": _RecordingYDL}))
    download_youtube("https://youtube.com/watch?v=abc123")
    assert seen[0]["js_runtimes"] == {"node": {}}
    assert seen[0]["noplaylist"] and seen[0]["quiet"] and seen[0]["merge_output_format"] == "mp4"


def test_yt_dlp_warnings_reach_our_log(caplog):
    with caplog.at_level(logging.WARNING, logger="ingest.youtube"):
        youtube.ydl_base_opts()["logger"].warning("n challenge solving failed")
    assert "yt-dlp: n challenge solving failed" in caplog.text
