FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Server/pyproject.toml depends on the two sibling packages, so all three
# directories must be present and installed together from the repo root.
COPY Research_Agent ./Research_Agent
COPY Outreach_Agent ./Outreach_Agent
COPY Server ./Server

RUN pip install -e Research_Agent -e Outreach_Agent -e Server

# Railway injects PORT. CSG_API_BASE is derived from it so the internal
# Research/Outreach hand-off URLs target the port this server listens on.
CMD ["sh", "-c", "export CSG_API_BASE=\"http://127.0.0.1:${PORT:-8000}\" && exec python -m uvicorn app:app --app-dir Server --host 0.0.0.0 --port ${PORT:-8000}"]
