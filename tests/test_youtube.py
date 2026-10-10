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
        return {"title": "  Weekly   Tech Sync \n", "upload_date": "20260802"}


class _FailingYDL(_FakeYDL):
    def download(self, urls):
        raise RuntimeError("network error")


def test_download_youtube_returns_id_path_and_title(monkeypatch):
    fake_module = type("m", (), {"YoutubeDL": _FakeYDL})
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", fake_module)

    video_id, path, title, upload_date = download_youtube("https://youtube.com/watch?v=abc123")
    assert path.exists()
    assert path.name == f"{video_id}.mp4"
    assert title == "Weekly Tech Sync"
    assert upload_date == "2026-08-02"  # the meeting's date in the final video's name


def test_download_youtube_without_an_upload_date(monkeypatch):
    class _NoDateYDL(_FakeYDL):
        def extract_info(self, url, download=True):
            self.download([url])
            return {"title": "x", "upload_date": "not-a-date"}

    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", type("m", (), {"YoutubeDL": _NoDateYDL}))
    assert download_youtube("https://youtube.com/watch?v=abc123").upload_date == ""


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
        monkeypatch.setattr(youtube, "get_settings", lambda: _settings(ffmpeg_bin=path))

    use_ffmpeg("ffmpeg")  # a bare name: yt-dlp searches PATH itself
    assert "ffmpeg_location" not in youtube.ydl_base_opts()

    ffmpeg = tmp_path / "bin" / "ffmpeg.exe"
    ffmpeg.parent.mkdir()
    ffmpeg.write_bytes(b"")
    use_ffmpeg(str(ffmpeg))
    assert youtube.ydl_base_opts()["ffmpeg_location"] == str(ffmpeg.parent)


def _settings(**overrides):
    return SimpleNamespace(**{"ffmpeg_bin": "ffmpeg", "ytdlp_cookies_file": "", "ytdlp_proxy": "", **overrides})


def test_base_opts_leave_cookies_and_proxy_off_by_default(monkeypatch):
    monkeypatch.setattr(youtube, "get_settings", lambda: _settings())
    opts = youtube.ydl_base_opts()
    assert "cookiefile" not in opts and "proxy" not in opts


def test_base_opts_pass_cookies_and_proxy_for_the_bot_check(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(youtube, "get_settings", lambda: _settings(
        ytdlp_cookies_file=str(cookies), ytdlp_proxy="http://user:pw@proxy.example:8080"))
    opts = youtube.ydl_base_opts()
    assert opts["cookiefile"] == str(cookies)
    assert opts["proxy"] == "http://user:pw@proxy.example:8080"


def test_base_opts_ignore_a_missing_cookies_file(monkeypatch, tmp_path):
    # a typo in the path must not fail every job before yt-dlp even starts
    monkeypatch.setattr(youtube, "get_settings", lambda: _settings(ytdlp_cookies_file=str(tmp_path / "nope.txt")))
    assert "cookiefile" not in youtube.ydl_base_opts()


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
