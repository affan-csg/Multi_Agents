from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("csg.diagnostics")

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


def log_event(event: str, **fields: Any) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **_sanitize(fields),
    }
    logger.info(json.dumps(payload, default=str, separators=(",", ":")))
