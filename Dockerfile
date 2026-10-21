# syntax=docker/dockerfile:1.7
#
# Multi-agent research assistant: one image, two roles.
#   API (default):  docker run -p 8000:8000 IMAGE
#   Worker:         docker run IMAGE research-worker      (requires JOB_BACKEND=redis)

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------------------
# Builder: build the wheel and install it (with its dependencies) into an isolated venv.
# ---------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip wheel --no-deps --wheel-dir /wheels . \
 && pip install /wheels/*.whl

# ---------------------------------------------------------------------------------------
# Runtime: slim base, only the venv is copied, runs as an unprivileged user.
# ---------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="multi-agent-research-assistant" \
      org.opencontainers.image.description="Production-ready multi-agent research assistant powered by Claude." \
      org.opencontainers.image.source="https://github.com/imranmali/multi-agent-research-assistant" \
      org.opencontainers.image.url="https://github.com/imranmali/multi-agent-research-assistant" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="imranmali"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HEALTHCHECK_URL="http://127.0.0.1:8000/healthz"

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER 10001:10001

EXPOSE 8000

# No curl in slim images: probe with the standard library. The worker overrides
# HEALTHCHECK_URL to its metrics endpoint (e.g. http://127.0.0.1:9100/metrics).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(os.environ.get('HEALTHCHECK_URL', 'http://127.0.0.1:8000/healthz'), timeout=4)" || exit 1

# No ENTRYPOINT on purpose: `docker run IMAGE research-worker` replaces the command.
# Trusted proxy IPs for --proxy-headers come from FORWARDED_ALLOW_IPS (uvicorn default: 127.0.0.1).
CMD ["uvicorn", "--factory", "research_assistant.api.app:create_app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--timeout-graceful-shutdown", "25"]
