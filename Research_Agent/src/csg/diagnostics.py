from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("csg.diagnostics")
DEFAULT_WORKER_VERSION = "gemini-only-shared-queue-v1"

_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "session",
    "bearer",
}


def _strip_query(url: str) -> str:
    if not url:
        return url
    try:
        parts = urlsplit(str(url))
    except Exception:
        return str(url)
    host = parts.hostname or ""
    if parts.port and host:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if any(part in str(key).lower() for part in _SENSITIVE_KEYS):
                continue
            out[str(key)] = _sanitize(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if "http://" in value or "https://" in value:
            return _strip_query(value)
        return stripped
    return value


def worker_version() -> str:
    return (
        os.environ.get("RESEARCH_WORKER_VERSION")
        or os.environ.get("K_REVISION")
        or os.environ.get("GIT_SHA")
        or DEFAULT_WORKER_VERSION
    )


def _write_event(row: dict[str, Any]) -> None:
    try:
        from . import db
        conn = db.connect()
        try:
            db.insert_diagnostic_event(conn, row)
        finally:
            conn.close()
    except Exception:
        pass


def log_event(event: str, **fields: Any) -> None:
    clean_fields = _sanitize(fields)
    created_at = datetime.now(timezone.utc).isoformat()
    version = worker_version()
    payload = {
        "ts": created_at,
        "event": event,
        "worker_version": version,
        **clean_fields,
    }
    logger.info(json.dumps(payload, default=str, separators=(",", ":")))
    row = {
        "event": event,
        "run_id": clean_fields.get("run_id"),
        "job_id": clean_fields.get("job_id"),
        "worker_id": clean_fields.get("worker_id"),
        "worker_version": version,
        "provider": clean_fields.get("provider"),
        "call_kind": clean_fields.get("call_kind"),
        "domain": clean_fields.get("domain"),
        "details": clean_fields,
        "created_at": created_at,
    }
    threading.Thread(target=_write_event, args=(row,), daemon=True).start()
