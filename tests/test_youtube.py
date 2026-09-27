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
        monkeypatch.setattr(youtube, "get_settings", lambda: SimpleNamespace(ffmpeg_bin=path, youtube_max_bytes=0, youtube_max_duration_s=0))

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


# ------------------------------------------------------------------ only YouTube, bounded
@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=30",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "  https://youtube.com/live/dQw4w9WgXcQ  ",
    "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
])
def test_youtube_links_are_accepted(url):
    from ingest.youtube import check_youtube_url

    assert check_youtube_url(url) == url.strip()


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8000/health",
    "https://example.com/video.mp4",
    "https://youtube.com.evil.example/watch?v=x",
    "https://evil.example/?u=https://youtube.com/watch?v=x",
    "https://user:pass@www.youtube.com/watch?v=x",
    "https://www.youtube.com:8443/watch?v=x",
    "file:///etc/passwd",
    "ftp://youtube.com/watch?v=x",
    "https://www.youtube.com/",
    "",
    "https://www.youtube.com/watch?v=" + "x" * 3000,
])
def test_other_links_are_refused(url):
    from ingest.youtube import check_youtube_url

    with pytest.raises(ValueError):
        check_youtube_url(url)


def test_ydl_runs_with_the_youtube_extractor_only_and_a_size_cap(monkeypatch):
    from core.config import get_settings
    from ingest.youtube import ydl_base_opts

    monkeypatch.setenv("YOUTUBE_MAX_BYTES", "1000")
    get_settings.cache_clear()
    opts = ydl_base_opts()
    assert opts["allowed_extractors"] == ["youtube"]
    assert opts["max_filesize"] == 1000
    assert opts["noplaylist"] is True


def test_real_ytdlp_refuses_non_youtube_urls_offline():
    """With the generic extractor disabled, yt-dlp can't be pointed at an
    internal address: it refuses before opening any connection."""
    yt_dlp = pytest.importorskip("yt_dlp")
    from ingest.youtube import ydl_base_opts

    with yt_dlp.YoutubeDL(ydl_base_opts()) as ydl:
        for url in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:9/x", "file:///etc/passwd"):
            with pytest.raises(yt_dlp.utils.DownloadError, match="No suitable extractor"):
                ydl.extract_info(url, download=False, process=False)


@pytest.mark.parametrize(("info", "refused"), [
    ({"is_live": True}, "live"),
    ({"live_status": "is_upcoming"}, "live"),
    ({"duration": 5 * 3600}, "hours long"),
    ({"duration": 3600, "live_status": "was_live"}, None),
    ({}, None),
])
def test_live_streams_and_very_long_videos_are_refused(info, refused):
    from ingest.youtube import _refuse_unsuitable, _Unsuitable

    if refused is None:
        assert _refuse_unsuitable(info) is None
    else:
        with pytest.raises(_Unsuitable, match=refused):
            _refuse_unsuitable(info)
