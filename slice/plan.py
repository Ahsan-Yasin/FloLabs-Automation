"""Pure frame arithmetic for rendering a list of kept ranges (plan §6.2).

Given kept ranges [s_i, e_i) in SOURCE frames and a dissolve of d frames
(h = d/2), the output must equal the hard-cut timeline exactly:
    total frames = Σ (e_i - s_i),   out(t) = t - s_i + O_i.
That holds because every dissolve is centred on the cut: each side is
extended (or trimmed) by h frames, and an xfade of d frames removes d.

Rendering one ffmpeg graph per ~20 ranges keeps RAM bounded, so the ranges
are grouped into batches. Where two batches meet at a cut, a separate "seam"
piece of d frames renders that one dissolve and both batches are trimmed by h
there. Very long ranges are split into several "atoms" joined by plain hard
splits (no visible change, just a concat boundary) so no single command
encodes more than `max_batch_frames` of content.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Literal

JoinKind = Literal["edge", "hard", "inner", "seam"]


@dataclass(frozen=True)
class Atom:
    range_index: int
    start: int  # source frame (inclusive)
    end: int  # source frame (exclusive)

    @property
    def frames(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class InputSpan:
    """What one ffmpeg input reads from the source (after extension/trim)."""

    start: int
    end: int

    @property
    def frames(self) -> int:
        return self.end - self.start


@dataclass
class VideoPart:
    kind: Literal["batch", "seam"]
    inputs: list[InputSpan]
    join: Literal["xfade", "concat", "none"]
    frames: int
    fade_frames: int = 0
    range_indices: list[int] = field(default_factory=list)

    def xfade_offsets(self) -> list[int]:
        """Frame offset of each dissolve start within this part's output."""
        offsets = []
        running = self.inputs[0].frames
        for span in self.inputs[1:]:
            offsets.append(running - self.fade_frames)
            running += span.frames - self.fade_frames
        return offsets


class PlanError(ValueError):
    pass


def split_into_atoms(ranges: list[tuple[int, int]], max_atom_frames: int) -> list[Atom]:
    atoms: list[Atom] = []
    for idx, (s, e) in enumerate(ranges):
        length = e - s
        if length <= 0:
            raise PlanError(f"range {idx} has non-positive length: [{s}, {e})")
        k = max(1, math.ceil(length / max_atom_frames)) if max_atom_frames > 0 else 1
        bounds = [s + (length * j) // k for j in range(k)] + [e]
        for a, b in itertools.pairwise(bounds):
            atoms.append(Atom(idx, a, b))
    return atoms


def check_ranges(ranges: list[tuple[int, int]], fade_frames: int, total_frames: int | None = None) -> None:
    """The invariants the half-extension relies on (the EDL builder enforces
    them; the renderer re-checks so a bad EDL fails loudly, not silently)."""
    d = fade_frames
    for i, (s, e) in enumerate(ranges):
        if e <= s:
            raise PlanError(f"range {i} is empty: [{s}, {e})")
        if s < 0 or (total_frames is not None and e > total_frames):
            raise PlanError(f"range {i} [{s}, {e}) is outside the source (0..{total_frames})")
        if d and e - s < d:
            raise PlanError(f"range {i} is {e - s} frames, shorter than the {d}-frame dissolve")
        if i:
            gap = s - ranges[i - 1][1]
            if gap <= 0:
                raise PlanError(f"ranges {i - 1} and {i} overlap or touch")
            if d and gap < d:
                raise PlanError(f"gap before range {i} is {gap} frames, shorter than the {d}-frame dissolve")


def plan_video(
    ranges: list[tuple[int, int]],
    fade_frames: int,
    batch_size: int = 20,
    max_batch_frames: int = 0,
) -> list[VideoPart]:
    """Split kept ranges (source frames) into batch and seam parts whose frame
    counts sum exactly to Σ(e - s). `fade_frames` must be even (0 = hard cuts)."""
    if not ranges:
        raise PlanError("nothing to render: no kept ranges")
    d = fade_frames
    if d % 2:
        raise PlanError(f"fade must be an even number of frames, got {d}")
    h = d // 2
    check_ranges(ranges, d)
    batch_size = max(1, batch_size)
    atoms = split_into_atoms(ranges, max_batch_frames)

    # Group atoms into batches. A boundary between atoms of the same range is a
    # hard split; between different ranges it is a cut (dissolve if d > 0).
    groups: list[list[Atom]] = [[atoms[0]]]
    boundaries: list[JoinKind] = []  # kind of the boundary BEFORE groups[k+1]
    content = atoms[0].frames
    for prev, atom in itertools.pairwise(atoms):
        same_range = atom.range_index == prev.range_index
        fits = len(groups[-1]) < batch_size and (max_batch_frames <= 0 or content + atom.frames <= max_batch_frames)
        if not same_range and fits:
            groups[-1].append(atom)
            content += atom.frames
            continue
        boundaries.append("hard" if same_range or d == 0 else "seam")
        groups.append([atom])
        content = atom.frames

    parts: list[VideoPart] = []
    for g, group in enumerate(groups):
        left: JoinKind = "edge" if g == 0 else boundaries[g - 1]
        right: JoinKind = "edge" if g == len(groups) - 1 else boundaries[g]
        spans: list[InputSpan] = []
        for k, atom in enumerate(group):
            a_join = left if k == 0 else "inner"
            z_join = right if k == len(group) - 1 else "inner"
            a = atom.start + {"edge": 0, "hard": 0, "inner": -h, "seam": h}[a_join]
            z = atom.end + {"edge": 0, "hard": 0, "inner": h, "seam": -h}[z_join]
            if z <= a:
                raise PlanError(f"atom {atom} collapses after trimming for the dissolve")
            spans.append(InputSpan(a, z))
        joins = len(spans) - 1
        join = "none" if joins == 0 else ("xfade" if d else "concat")
        frames = sum(sp.frames for sp in spans) - joins * d
        parts.append(VideoPart("batch", spans, join, frames, d, sorted({a.range_index for a in group})))
        if g < len(groups) - 1 and boundaries[g] == "seam":
            e = group[-1].end
            s_next = groups[g + 1][0].start
            parts.append(
                VideoPart("seam", [InputSpan(e - h, e + h), InputSpan(s_next - h, s_next + h)], "xfade", d, d,
                          [group[-1].range_index, groups[g + 1][0].range_index])
            )

    expected = sum(e - s for s, e in ranges)
    got = sum(p.frames for p in parts)
    if got != expected:  # pragma: no cover — arithmetic invariant
        raise PlanError(f"plan produces {got} frames, expected {expected}")
    return parts


def output_offsets(ranges: list[tuple[int, int]]) -> list[int]:
    """O_i: output frame where kept range i starts (hard-cut timeline)."""
    offsets, running = [], 0
    for s, e in ranges:
        offsets.append(running)
        running += e - s
    return offsets
