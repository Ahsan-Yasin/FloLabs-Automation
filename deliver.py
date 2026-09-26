"""Everything after the decisions: render the videos, write the transcripts,
chapters and report, then manifest.json + bundle.zip (plan §6.4, D9-D12, D18).

The zip the owner gets for each meeting:
    final.mp4            highlights reel, title card, then the cleaned meeting
    highlights.mp4       the reel on its own
    shorts/short_NN.mp4  vertical shorts (+ .srt captions, shorts.json)
    removed.mp4          every cut of 1 s or more (not pure silences), labelled with time + reason
    report.pdf           what was removed, when and why; highlights; shorts; chapters
    transcript_removed.txt/.json   the removed text with original timestamps
    transcript_clean.txt/.json     what final.mp4 says, with final.mp4 times
    chapters.txt         YouTube chapters for final.mp4
    manifest.json        sizes, sha256, durations, status of every file

Mandatory: final.mp4, both transcripts, manifest, bundle. Anything else that
fails is recorded (status + reason + a warning) and the job still succeeds.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from bundle.package import fill_file_info, sha256_file, write_bundle, write_manifest
from core.config import get_settings
from core.errors import InsufficientDisk, is_fatal
from core.logging import get_logger
from core.models import ArtifactInfo, JobRecord, JobStatus, Moment, RenderManifest, Word
from core.timeline import parse_rate
from core.version import PIPELINE_VERSION
from decide.chapters import ChapterResult, finalize_chapters, format_chapters
from outputs import RenderInputs, RenderOutputs, render_outputs
from report.pdf import ReportData, build_report
from report.text import (
    clean_lines,
    removed_entries,
    write_clean_transcript,
    write_removed_transcript,
)
from slice.fonts import find_font
from slice.transcript import remap_transcript

logger = get_logger(__name__)

ChapterFn = Callable[[list[Word], float], ChapterResult]
ZIP_HEADROOM_BYTES = 100 * 1024**2


@dataclass
class DeliverInputs:
    render: RenderInputs
    reel: list[Moment] = field(default_factory=list)
    # None = chapters disabled
    chapter_fn: ChapterFn | None = None


def deliver(job: JobRecord, update: Callable[[JobRecord], None], inp: DeliverInputs) -> None:
    """Runs the rest of the job; sets job.status DONE at the end. Raises on a
    mandatory failure (the caller marks the job failed)."""
    settings = get_settings()
    job_dir = inp.render.job_dir

    def set_status(status: JobStatus) -> None:
        job.status = status
        job.progress_current = job.progress_total = 0
        update(job)

    def on_progress(current: int, total: int) -> None:
        job.progress_current, job.progress_total = current, total
        update(job)

    with _stage(job, "rendering"):
        out = render_outputs(inp.render, set_status=set_status, on_progress=on_progress)
    job.warnings.extend(out.warnings)
    job.artifacts = dict(out.artifacts)
    job.output_video_path = str(out.final_path)
    job.final_offset_s = round(out.final_offset_s, 6)
    for name, secs in out.timings.items():
        job.stage_timings[f"render_{name}"] = secs
    _write_json(job_dir / "render_manifest.json", out.cleaned.model_dump())
    job.render_manifest_path = str(job_dir / "render_manifest.json")
    job.progress_current = job.progress_total = 0

    words = inp.render.words
    # A sentence whose caption span straddles a cut pause was still said.
    # Every pause cut counts, not only the removed ranges that are pure
    # silence: a pause at the end of a kept sentence merges with the removed
    # sentence after it into one removed range.
    silence = inp.render.edl.silence_cuts + [(r.start, r.end) for r in inp.render.removed if r.silence]
    clean_words = remap_transcript(words, manifest=out.cleaned, silence=silence)
    # what is said in the reel at the start of final.mp4 (its own timeline = final's)
    reel_words = []
    if out.highlights is not None:
        reel_silence = inp.render.reel_edl.silence_cuts if inp.render.reel_edl is not None else []
        reel_words = remap_transcript(words, manifest=out.highlights, silence=reel_silence)
    _write_json(job_dir / "clean_transcript.json", [w.model_dump() for w in clean_words])
    job.clean_transcript_path = str(job_dir / "clean_transcript.json")

    with _stage(job, "reporting"):
        set_status(JobStatus.REPORTING)
        meeting = inp.render.title  # the same name as on the title card ("" = none known)
        source_s = inp.render.media.video_duration
        entries = removed_entries(inp.render.removed, words, out.removed, silence=silence)
        write_removed_transcript(job_dir / "transcript_removed.txt", job_dir / "transcript_removed.json", entries,
                                 meeting=meeting, source_duration_s=source_s)
        write_clean_transcript(job_dir / "transcript_clean.txt", job_dir / "transcript_clean.json",
                               clean_lines(clean_words, out.final_offset_s, reel_words), meeting=meeting,
                               offset_s=out.final_offset_s, final_duration_s=out.final_duration_s)
        for name in ("transcript_clean.txt", "transcript_clean.json", "transcript_removed.txt",
                     "transcript_removed.json"):
            job.artifacts[name] = ArtifactInfo(path=name, kind="json" if name.endswith(".json") else "text",
                                               mandatory=True)

        chapters_text = _chapters(job, job_dir, inp.chapter_fn, clean_words, out)
        highlights = _reel_rows(out.highlights, inp.reel)
        shorts_rows = [(r.path.relative_to(job_dir).as_posix(), r.clip.start, r.clip.end, r.clip.title, r.clip.hook)
                       for r in out.shorts]
        if out.shorts:
            (job_dir / "shorts").mkdir(exist_ok=True)
            _write_json(job_dir / "shorts" / "shorts.json", [
                {"file": row[0], "source_start": round(row[1], 3), "source_end": round(row[2], 3),
                 "duration_s": round(r.duration_s, 3), "title": r.clip.title, "hook": r.clip.hook,
                 "category": r.clip.category, "captions": row[0].replace(".mp4", ".srt")}
                for row, r in zip(shorts_rows, out.shorts, strict=True)])
            job.artifacts["shorts/shorts.json"] = ArtifactInfo(path="shorts/shorts.json", kind="json")

        if settings.report_enabled:
            try:
                fill_file_info(job_dir, job.artifacts, hashes=False)  # sizes for the report
                build_report(job_dir / "report.pdf", ReportData(
                    meeting=meeting,
                    job_id=job.job_id,
                    version=PIPELINE_VERSION,
                    created=(job.created_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC"),
                    source_name=Path(inp.render.source).name,
                    source_duration_s=source_s,
                    final_duration_s=out.final_duration_s,
                    cleaned_duration_s=out.cleaned.measured_duration_s or 0.0,
                    highlights_duration_s=_duration(out.highlights),
                    card_s=out.card_s,
                    removed=entries,
                    merged_back_count=inp.render.edl.merged_gap_count,
                    merged_back_s=inp.render.edl.merged_gap_seconds,
                    highlights=highlights,
                    shorts=shorts_rows,
                    chapters=chapters_text,
                    artifacts=[(name, a.status, a.duration_s, a.bytes, a.reason)
                               for name, a in _ordered(job.artifacts).items()],
                    warnings=job.warnings,
                    llm_usage=job.llm_usage,
                    stage_timings=job.stage_timings,
                ), font=find_font(), bold_font=find_font(bold=True))
                job.artifacts["report.pdf"] = ArtifactInfo(path="report.pdf", kind="pdf")
            except Exception as exc:
                if is_fatal(exc):
                    raise
                logger.exception("report.pdf failed")
                job.artifacts["report.pdf"] = ArtifactInfo(path="report.pdf", kind="pdf", status="failed",
                                                           reason=str(exc)[:300])
                job.warnings.append(f"report.pdf failed: {exc}")

    with _stage(job, "bundling"):
        set_status(JobStatus.BUNDLING)
        job.artifacts = _ordered(job.artifacts)
        fill_file_info(job_dir, job.artifacts)
        needed = sum(a.bytes or 0 for a in job.artifacts.values() if a.status == "ok") + ZIP_HEADROOM_BYTES
        free = shutil.disk_usage(job_dir).free
        if free < needed:
            raise InsufficientDisk(f"only {free / 1024**3:.1f} GiB free, the bundle needs {needed / 1024**3:.1f} GiB")
        manifest = _manifest(job, inp, out, highlights, shorts_rows, entries)
        write_manifest(job_dir / "manifest.json", manifest)
        bundle = write_bundle(job_dir, job.artifacts, job_dir / "bundle.zip", extra=["manifest.json"])
        info = ArtifactInfo(path="manifest.json", kind="json", mandatory=True)
        job.artifacts["manifest.json"] = info
        fill_file_info(job_dir, {"manifest.json": info})
        job.bundle_path = str(bundle)
        job.bundle_bytes = bundle.stat().st_size
        job.bundle_sha256 = sha256_file(bundle)

    _cleanup(job, job_dir, Path(inp.render.source))
    job.status = JobStatus.DONE
    job.progress_current = job.progress_total = 0
    update(job)


# ------------------------------------------------------------------ helpers


def _chapters(job: JobRecord, job_dir: Path, chapter_fn: ChapterFn | None, clean_words: list[Word],
              out: RenderOutputs) -> str:
    """Topic chapters on the cleaned meeting, placed on final.mp4 (after the
    reel: shifted, with "00:00 Highlights" first). Optional."""
    if chapter_fn is None:
        return ""
    cleaned_s = out.cleaned.measured_duration_s or 0.0
    try:
        result = chapter_fn(clean_words, cleaned_s)
    except Exception as exc:
        if is_fatal(exc):
            raise
        job.warnings.append(f"chapters skipped: {exc}")
        job.artifacts["chapters.txt"] = ArtifactInfo(path="chapters.txt", kind="text", status="failed",
                                                     reason=str(exc)[:300])
        return ""
    if not result.ok:
        # the problems are phrased for the model's retry ("... — split it where
        # the topic changes"); the owner only needs the part before the dash
        reason = "; ".join(p.split(" — ")[0] for p in result.problems)
        job.warnings.append("chapters skipped: " + reason)
        job.artifacts["chapters.txt"] = ArtifactInfo(path="chapters.txt", kind="text", status="skipped",
                                                     reason=reason[:300])
        return ""
    entries, problems = finalize_chapters(result.chapters, final_duration_s=out.final_duration_s,
                                          reel_s=out.final_offset_s)
    if problems:
        job.warnings.append("chapters skipped: " + "; ".join(problems))
        job.artifacts["chapters.txt"] = ArtifactInfo(path="chapters.txt", kind="text", status="skipped",
                                                     reason="; ".join(problems)[:300])
        return ""
    text = format_chapters(entries, out.final_duration_s)
    _write_json(job_dir / "chapters.json", {
        "timeline": "final",
        "cleaned_starts_at_s": round(out.final_offset_s, 3),
        "chapters_cleaned_timeline": [c.model_dump() for c in result.chapters],
        "entries": [{"t": t, "title": title} for t, title in entries],
    })
    (job_dir / "chapters.txt").write_text(text, encoding="utf-8")
    job.chapters_path = str(job_dir / "chapters.txt")
    job.artifacts["chapters.txt"] = ArtifactInfo(path="chapters.txt", kind="text")
    return text


def _reel_rows(manifest: RenderManifest | None, reel: list[Moment]) -> list[tuple[float, float, float, str]]:
    """(time in final.mp4, source start, source end, title) per reel clip. The
    reel is the first thing in final.mp4, so its own timeline is final's.
    Pieces of one moment split by cut pauses are one clip."""
    if manifest is None:
        return []
    fps = parse_rate(manifest.fps)
    rows: list[tuple[float, float, float, str]] = []
    last_id = None
    for p in manifest.pieces:
        s, e = float(Fraction(p.src_start_frame) / fps), float(Fraction(p.src_end_frame) / fps)
        best = max(reel, key=lambda m: min(e, m.end) - max(s, m.start), default=None)
        if best is None or min(e, best.end) <= max(s, best.start):
            best = None
        if best is not None and rows and best.id == last_id:
            rows[-1] = (*rows[-1][:2], e, rows[-1][3])
            continue
        title = (best.title or best.category.replace("_", " ")) if best is not None else ""
        rows.append((float(Fraction(p.out_start_frame) / fps), s, e, title))
        last_id = best.id if best is not None else None
    return rows


def _duration(manifest: RenderManifest | None) -> float:
    if manifest is None:
        return 0.0
    return float(Fraction(manifest.expected_frames) / parse_rate(manifest.fps))


_ORDER = ("final.mp4", "highlights.mp4", "removed.mp4", "shorts/", "report.pdf", "transcript_removed", "transcript_clean",
          "chapters.txt", "cleaned.mp4")


def _ordered(artifacts: dict[str, ArtifactInfo]) -> dict[str, ArtifactInfo]:
    def key(name: str):
        for i, prefix in enumerate(_ORDER):
            if name.startswith(prefix):
                return (i, name)
        return (len(_ORDER), name)

    return {k: artifacts[k] for k in sorted(artifacts, key=key)}


def _manifest(job: JobRecord, inp: DeliverInputs, out: RenderOutputs, highlights, shorts_rows, entries) -> dict:
    media = inp.render.media
    removed = inp.render.removed
    return {
        "job_id": job.job_id,
        "title": inp.render.title,
        "pipeline_version": PIPELINE_VERSION,
        "created_at": job.created_at,
        "finished_at": datetime.now(UTC),
        "source": {"name": Path(inp.render.source).name, "duration_s": round(media.video_duration, 3),
                   "fps": f"{media.fps.numerator}/{media.fps.denominator}", "width": media.width,
                   "height": media.height, "transcript": job.transcript_source},
        "final": {"duration_s": round(out.final_duration_s, 3), "frames": out.final_frames,
                  "cleaned_starts_at_s": round(out.final_offset_s, 3),
                  "highlights_s": round(_duration(out.highlights), 3), "title_card_s": round(out.card_s, 3)},
        "artifacts": {name: a.model_dump(exclude={"on_disk"}) for name, a in job.artifacts.items()},
        "highlights": [{"final_at_s": round(a, 3), "source_start": round(s, 3), "source_end": round(e, 3),
                        "title": t} for a, s, e, t in highlights],
        "shorts": [{"file": f, "source_start": round(s, 3), "source_end": round(e, 3), "title": t, "hook": h}
                   for f, s, e, t, h in shorts_rows],
        "removed": {
            "cuts": len(removed),
            "seconds": round(sum(r.end - r.start for r in removed), 3),
            "in_removed_video": sum(1 for e in entries if e.removed_video_at is not None),
            "transcript_only": sum(1 for e in entries if e.removed_video_at is None),
            "silence_cuts": sum(1 for r in removed if r.silence),
            "silence_s": round(sum(r.end - r.start for r in removed if r.silence), 3),
            "merged_back_count": inp.render.edl.merged_gap_count,
            "merged_back_s": round(inp.render.edl.merged_gap_seconds, 3),
        },
        "warnings": job.warnings,
        "stage_timings": job.stage_timings,
        "llm_usage": job.llm_usage,
    }


def _cleanup(job: JobRecord, job_dir: Path, source: Path) -> None:
    """Disk hygiene after the zip (plan §6.5): on EC2 only the zip, the
    manifest and the job's own records stay; a source recording inside the
    job folder goes when the job is done."""
    settings = get_settings()
    if not settings.serve_individual_artifacts:
        for name, info in job.artifacts.items():
            if name == "manifest.json" or info.status != "ok":
                continue
            (job_dir / info.path).unlink(missing_ok=True)
            info.on_disk = False
        shutil.rmtree(job_dir / "shorts", ignore_errors=True)
    if settings.delete_source_when_done:
        for path in (source, Path(job.native_transcript_path) if job.native_transcript_path else None):
            if path is not None and _inside(path, job_dir):
                path.unlink(missing_ok=True)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


@contextmanager
def _stage(job: JobRecord, name: str):
    t0 = time.monotonic()
    try:
        yield
    finally:
        job.stage_timings[name] = round(time.monotonic() - t0, 2)


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
