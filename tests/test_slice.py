import subprocess

from core.models import EditDecisionList, EDLRange
from slice import ffmpeg_wrapper as ffmpeg_wrapper_module
from slice.pipeline import render_output


def _fake_run(cmd, **kwargs):
    if "-skip_frame" in cmd:
        # keyframe probe: pretend keyframes exist every 5s
        stdout = "\n".join(str(float(t)) for t in range(0, 200, 5))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")
    # any ffmpeg extract/concat call: write a tiny real file at the destination
    # so downstream shutil.copyfile / concat logic has something real to touch
    dest = cmd[-1]
    with open(dest, "wb") as f:
        f.write(b"fake video bytes")
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


def test_render_output_reports_progress_per_clip(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_wrapper_module.subprocess, "run", _fake_run)

    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake source")
    edl = EditDecisionList(
        ranges=[EDLRange(start=0.0, end=10.0), EDLRange(start=20.0, end=30.0), EDLRange(start=40.0, end=50.0)],
        source_duration=60.0,
    )
    out_path = tmp_path / "output.mp4"
    work_dir = tmp_path / "tmp"

    progress_calls = []
    render_output(source, edl, out_path, work_dir, on_progress=lambda c, t: progress_calls.append((c, t)))

    assert progress_calls == [(1, 3), (2, 3), (3, 3)]
    assert out_path.exists()


def test_render_output_works_without_progress_callback(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_wrapper_module.subprocess, "run", _fake_run)

    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake source")
    edl = EditDecisionList(ranges=[EDLRange(start=0.0, end=10.0)], source_duration=10.0)
    out_path = tmp_path / "output.mp4"
    work_dir = tmp_path / "tmp"

    render_output(source, edl, out_path, work_dir)
    assert out_path.exists()


def test_render_output_cleans_up_intermediate_clips(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_wrapper_module.subprocess, "run", _fake_run)

    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake source")
    edl = EditDecisionList(
        ranges=[EDLRange(start=0.0, end=10.0), EDLRange(start=20.0, end=30.0)], source_duration=30.0
    )
    out_path = tmp_path / "output.mp4"
    work_dir = tmp_path / "tmp"

    render_output(source, edl, out_path, work_dir)

    assert not work_dir.exists()
