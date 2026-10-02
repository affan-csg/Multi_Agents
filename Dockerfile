FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY Research_Agent ./Research_Agent
COPY Outreach_Agent ./Outreach_Agent
COPY Server ./Server

# Install in dependency order: the two agents first, then Server, which
# depends on both. Editable installs keep the sibling layout that
# Server/app.py relies on (it resolves each agent's directory from its own path).
RUN pip install -e ./Research_Agent \
    && pip install -e ./Outreach_Agent \
    && pip install -e ./Server

# Railway provides PORT; bind to 0.0.0.0 so the service is reachable.
CMD ["sh", "-c", "exec python -m uvicorn app:app --app-dir Server --host 0.0.0.0 --port ${PORT:-8000}"]
