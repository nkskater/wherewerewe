# One image, two processes: `wherewerewe run` starts the API server and the worker.
# Build from the repository root:  docker build -f docker/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    CONFIG_DIR=/config \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.13.6

WORKDIR /app

# WHISPER=false builds a much smaller image without the speech-to-text fallback.
ARG WHISPER=true
ARG TARGETARCH

COPY backend/pyproject.toml backend/uv.lock ./
RUN extras="--extra postgres"; \
    if [ "$WHISPER" = "true" ]; then extras="$extras --extra whisper"; fi; \
    uv sync --frozen --no-dev --no-install-project $extras

COPY backend/wherewerewe ./wherewerewe
RUN extras="--extra postgres"; \
    if [ "$WHISPER" = "true" ]; then extras="$extras --extra whisper"; fi; \
    uv sync --frozen --no-dev $extras

# CUDA 12 / cuDNN 9 runtime libraries for faster-whisper on NVIDIA GPUs (x86_64 only;
# on arm64 Whisper runs on the CPU).
RUN if [ "$WHISPER" = "true" ] && [ "$TARGETARCH" = "amd64" ]; then \
        uv pip install --python /app/.venv/bin/python --no-cache \
            "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*"; \
    fi
ENV LD_LIBRARY_PATH=/app/.venv/lib/python3.12/site-packages/nvidia/cublas/lib:/app/.venv/lib/python3.12/site-packages/nvidia/cudnn/lib

COPY backend/tests/spoiler_eval/fixtures /app/spoiler_eval
ENV EVAL_FIXTURES=/app/spoiler_eval

VOLUME /config
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health/live', timeout=4)"

CMD ["wherewerewe", "run"]
