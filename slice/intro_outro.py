"""Find the owner's intro / outro clips for final.mp4.

The owner drops two videos into the `intro_outro/` folder and names them
however the editor exported them ("CTD - Opening.mp4", "FloLabs - Widescreen
outro.mp4"), so the role comes from words in the file name:

    intro: intro / opening / open / start
    outro: outro / ending / closing / end

Words, not substrings ("Frontend demo.mp4" is neither), split on anything
that is not a letter or digit and on camelCase ("FloLabsIntro" -> flo labs
intro). With exactly two videos of which one is recognised, the other takes
the remaining role. Two intros -> the alphabetically first, with a warning.
INTRO_FILE / OUTRO_FILE name a file explicitly instead. A missing folder or
file only means that part is left out: final.mp4 never depends on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from core.config import PROJECT_ROOT, Settings
from core.logging import get_logger

logger = get_logger(__name__)

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm"})
ROLE_WORDS = {
    "intro": frozenset({"intro", "opening", "open", "start"}),
    "outro": frozenset({"outro", "ending", "closing", "end"}),
}
ROLES = ("intro", "outro")


@dataclass
class IntroOutro:
    intro: Path | None = None
    outro: Path | None = None
    # for the job's warnings (the owner should know a clip was guessed or ignored)
    notes: list[str] = field(default_factory=list)


def project_path(value: str | Path) -> Path:
    """A setting's path: absolute as given, relative to the project folder."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def file_words(name: str) -> set[str]:
    stem = Path(name).stem
    return {w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", stem)}


def role_of(name: str) -> str | None:
    """"intro", "outro" or None (no role word, or words of both roles)."""
    words = file_words(name)
    roles = [role for role in ROLES if words & ROLE_WORDS[role]]
    return roles[0] if len(roles) == 1 else None


def find_intro_outro(settings: Settings, enabled: bool | None = None) -> IntroOutro:
    """The clips to put around final.mp4. `enabled` is the job's own choice
    (None = INTRO_OUTRO_ENABLED)."""
    found = IntroOutro()
    if not (settings.intro_outro_enabled if enabled is None else enabled):
        logger.info("intro/outro: switched off")
        return found
    explicit = {"intro": settings.intro_file.strip(), "outro": settings.outro_file.strip()}
    for role, name in explicit.items():
        if name:
            path = project_path(name)
            if path.is_file():
                setattr(found, role, path)
            else:
                logger.info("intro/outro: %s file %s not found, no %s", role, path, role)
    wanted = [role for role in ROLES if not explicit[role]]
    folder = project_path(settings.intro_outro_dir)
    if wanted and folder.is_dir():
        try:
            _from_folder(folder, wanted, found)
        except OSError as exc:  # an unreadable folder must not cost the job
            logger.warning("intro/outro: cannot read %s: %s", folder, exc)
            found.notes.append(f"the {folder.name} folder could not be read, no intro/outro: {exc}")
    elif wanted:
        logger.info("intro/outro: no folder %s, no %s", folder, " / ".join(wanted))
    logger.info("intro/outro: intro %s, outro %s", found.intro.name if found.intro else "none",
                found.outro.name if found.outro else "none")
    return found


def _from_folder(folder: Path, wanted: list[str], found: IntroOutro) -> None:
    videos = sorted((p for p in folder.iterdir()
                     if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith((".", "~"))),
                    key=lambda p: p.name.lower())
    by_role: dict[str, list[Path]] = {role: [p for p in videos if role_of(p.name) == role] for role in ROLES}
    unnamed = [p for p in videos if role_of(p.name) is None]
    if len(videos) == 2 and len(unnamed) == 1:
        # "Opening.mp4" + "FloLabs.mp4": the second can only be the outro
        missing = next(role for role in ROLES if not by_role[role])
        by_role[missing] = unnamed
        unnamed = []
        logger.info("intro/outro: %s taken as the %s (the other file is the %s)", by_role[missing][0].name,
                    missing, "outro" if missing == "intro" else "intro")
    for role in wanted:
        picks = by_role[role]
        if len(picks) > 1:
            found.notes.append(f"{len(picks)} {role} videos in the {folder.name} folder "
                               f"({', '.join(p.name for p in picks)}); used {picks[0].name}")
        if picks:
            setattr(found, role, picks[0])
    if unnamed:
        names = ", ".join(p.name for p in unnamed)
        found.notes.append(f"{names} in the {folder.name} folder not used: put \"intro\" or \"outro\" in the "
                           "file name")
