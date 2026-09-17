import shutil
from pathlib import Path

from core.logging import get_logger
from core.models import EditDecisionList

from .ffmpeg_wrapper import (
    choose_mode,
    concat_clips,
    extract_segment,
    probe_keyframe_timestamps,
)

logger = get_logger(__name__)


def render_output(source: Path, edl: EditDecisionList, out_path: Path, work_dir: Path) -> Path:
    """Extract each KEEP range and concatenate them into the final output (section 4).

    Intermediate per-segment clips are always cleaned up, even on failure.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    keyframes = probe_keyframe_timestamps(source)
    clip_paths: list[Path] = []

    try:
        for i, r in enumerate(edl.ranges):
            mode = choose_mode(r.start, keyframes)
            clip_path = work_dir / f"clip_{i:04d}{source.suffix}"
            extract_segment(source, r.start, r.end, clip_path, mode)
            clip_paths.append(clip_path)
            logger.info("sliced range %d [%.2f, %.2f) mode=%s", i, r.start, r.end, mode)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        if len(clip_paths) == 1:
            shutil.copyfile(clip_paths[0], out_path)
        else:
            filelist_path = work_dir / "concat_list.txt"
            concat_clips(clip_paths, out_path, filelist_path)
    finally:
        for clip in clip_paths:
            clip.unlink(missing_ok=True)
        (work_dir / "concat_list.txt").unlink(missing_ok=True)
        try:
            work_dir.rmdir()
        except OSError:
            pass  # not empty / already gone — fine, not worth failing the job over

    return out_path
