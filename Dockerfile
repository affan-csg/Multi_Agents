FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CSG_RUNTIME_DIR=/tmp/csg

WORKDIR /app

COPY Research_Agent ./Research_Agent
COPY Outreach_Agent ./Outreach_Agent
COPY Server ./Server

RUN pip install --no-cache-dir ./Research_Agent ./Outreach_Agent ./Server \
    && groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /tmp/csg /tmp/csg/cache /tmp/csg/exports \
    && chown -R 1000:1000 /app /tmp/csg

USER appuser

EXPOSE 8000

# Railway injects PORT. CSG_API_BASE is derived from it so the internal
# Research/Outreach hand-off URLs target the port this server listens on.
# Cloud Run's dedicated Research worker uses this same image with command
# override `python -m csg.worker` and its own instance-based service settings.
CMD ["sh", "-c", "export CSG_API_BASE=\"http://127.0.0.1:${PORT:-8000}\" && exec python -m uvicorn app:app --app-dir Server --host 0.0.0.0 --port ${PORT:-8000}"]
