# syntax=docker/dockerfile:1
# Highlight Cutter: the website, the API and the render worker in one image.
#
# WhisperX (torch) is deliberately not included: jobs reuse YouTube captions or
# an uploaded .vtt/.srt. Set REQUIRE_NATIVE_TRANSCRIPT=true in .env so a job
# without one fails fast instead of trying to transcribe.
#
# Run exactly ONE app process per storage folder: the job queue lives in
# memory (one job renders at a time), so never add uvicorn --workers.

# Debian trixie: its ffmpeg is 7.1. The renderer needs ffmpeg 7+ (it passes filter
# graphs as files with -/filter_complex); bookworm's 5.1 can't render.
ARG PYTHON_IMAGE=python:3.12-slim-trixie
# yt-dlp needs a JavaScript runtime for YouTube. Pin a tag (e.g. bin-2.5.6) or a
# digest for reproducible builds.
ARG DENO_IMAGE=denoland/deno:bin

# ---------------------------------------------------------------- dependencies
FROM ${PYTHON_IMAGE} AS build
# git: yt-dlp is installed from its repository (requirements-server.txt)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements-server.txt .
RUN pip install -r requirements-server.txt

FROM ${DENO_IMAGE} AS deno

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}
# ffmpeg renders; DejaVu draws title cards and captions; curl is the healthcheck;
# sqlite3 is handy for inspecting the database by hand
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core curl sqlite3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STORAGE_DIR=/app/storage \
    XDG_CACHE_HOME=/app/storage/.cache

WORKDIR /app
# the code stays root-owned and read-only for the app user; only storage/ is writable
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin hc \
    && mkdir -p /app/storage \
    && chown hc:hc /app/storage
COPY . .
USER hc

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health > /dev/null || exit 1
# --proxy-headers: Caddy (the only thing that can reach this port) passes the
# real client address and https scheme along; it drops forged X-Forwarded-*
# headers from clients by default.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
