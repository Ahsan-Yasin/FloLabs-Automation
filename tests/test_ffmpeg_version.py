"""The renderer needs ffmpeg 7+; the app says so at startup when it isn't."""

import pytest

from api.main import ffmpeg_too_old


@pytest.mark.parametrize(("version", "too_old"), [
    ("5.1.9-0+deb12u1", True),     # Debian bookworm
    ("6.1.1-3ubuntu5", True),      # Ubuntu 24.04
    ("7.1.1-1+b1", False),         # Debian trixie
    ("9.0.1", False),
    ("N-117000-g1234abcd", False),  # git builds: unknown, allowed
    (None, False),
    ("", False),
])
def test_ffmpeg_too_old(version, too_old):
    assert ffmpeg_too_old(version) is too_old
