import pytest

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


class _FailingYDL(_FakeYDL):
    def download(self, urls):
        raise RuntimeError("network error")


def test_download_youtube_returns_id_and_path(monkeypatch):
    fake_module = type("m", (), {"YoutubeDL": _FakeYDL})
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", fake_module)

    video_id, path = download_youtube("https://youtube.com/watch?v=abc123")
    assert path.exists()
    assert path.name == f"{video_id}.mp4"


def test_download_youtube_wraps_failures(monkeypatch):
    fake_module = type("m", (), {"YoutubeDL": _FailingYDL})
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", fake_module)

    with pytest.raises(YoutubeDownloadError):
        download_youtube("https://youtube.com/watch?v=abc123")
