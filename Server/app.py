"""Single-port composition layer.

Mounts the Research Agent's app (csg.api:app) at /research and the Outreach
Agent's app (outreach.api:app) at /outreach onto one FastAPI instance, so
both run under one uvicorn process/port instead of two. This file is pure
composition/port-routing -- no business logic, no route handlers, no model
changes. Both agents' code, databases, and API behavior are otherwise
untouched; each database uses its own scoped environment variables.

Why this file has to run before either agent is imported
----------------------------------------------------------
Both `csg.config` and `outreach.config` call `load_dotenv(PROJECT_ROOT/.env)`
at import time, into the same process-wide `os.environ`. The two projects'
`.env` files hold different Supabase projects, and merging their databases
is explicitly out of scope. So this module:

1. Reads each agent's own `.env` file itself (via `dotenv_values`, which
    does NOT touch `os.environ`) and publishes the already-scoped
    `RESEARCH_SUPABASE_*` / `OUTREACH_SUPABASE_*` values. Render can provide
    all four variables directly in the process environment, which take
    precedence over these files.
2. Points each agent's cross-agent HTTP base (`OUTREACH_API_BASE` read by
   Research, `RESEARCH_API_BASE` read by Outreach) at this same server's own
   prefixed paths, so the existing hand-off code (discover.py's
   _handoff_to_outreach, outreach's research_client.py) keeps working
   without any code change -- only the URL it was already configured with
   changes.
3. Does this before `csg.api` / `outreach.api` are imported, because each
   module's own `load_dotenv()` call (inside csg.config / outreach.config)
   runs at *their* import time and must see the final values already in
   `os.environ` -- python-dotenv defaults to not overriding a var that's
   already set, so setting these first is what makes them stick.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from dotenv import dotenv_values
from fastapi import FastAPI

logger = logging.getLogger("csg_server")

SERVER_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SERVER_ROOT.parent
RESEARCH_ENV = REPO_ROOT / "Research_Agent" / ".env"
OUTREACH_ENV = REPO_ROOT / "Outreach_Agent" / ".env"

# Keys both agents may independently set that are meant to be the same value
# (shared LLM providers) -- not a hard error if they differ (each project's
# .env is free to use its own key), just something worth a heads-up since a
# silent split is easy to miss.
_SHARED_KEY_NAMES = (
    "GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
    "ANTHROPIC_API_KEY", "GROQ_API_KEY", "TAVILY_API_KEY",
)


def _configure_env() -> None:
    """Set the scoped Supabase vars and prefixed cross-agent API bases in
    os.environ, before either agent package is imported. Idempotent, and
    never overrides a value the environment already had (matches
    python-dotenv's own default override=False behavior, so an operator who
    sets these directly, e.g. in a container, is always respected)."""
    research_env = dotenv_values(RESEARCH_ENV) if RESEARCH_ENV.exists() else {}
    outreach_env = dotenv_values(OUTREACH_ENV) if OUTREACH_ENV.exists() else {}

    _set_default("RESEARCH_SUPABASE_URL", research_env.get("RESEARCH_SUPABASE_URL"))
    _set_default("RESEARCH_SUPABASE_SERVICE_KEY", research_env.get("RESEARCH_SUPABASE_SERVICE_KEY"))
    _set_default("OUTREACH_SUPABASE_URL", outreach_env.get("OUTREACH_SUPABASE_URL"))
    _set_default("OUTREACH_SUPABASE_SERVICE_KEY", outreach_env.get("OUTREACH_SUPABASE_SERVICE_KEY"))

    # Also republish each agent's own other .env values (Gemini/Anthropic/
    # SMTP/etc keys) into os.environ so this single process has everything
    # both agents need -- load_dotenv(override=False) inside each agent's
    # config module would otherwise see an *empty* environment for anything
    # not already set here (there is no longer a per-agent process whose own
    # .env load would supply them).
    for env in (research_env, outreach_env):
        for key, value in env.items():
            _set_default(key, value)

    for key in _SHARED_KEY_NAMES:
        r_val, o_val = research_env.get(key), outreach_env.get(key)
        if r_val and o_val and r_val != o_val:
            logger.warning(
                "Server: %s differs between Research_Agent/.env and Outreach_Agent/.env "
                "-- the single server process uses whichever one os.environ resolves "
                "first; if that matters, set them to the same value.",
                key,
            )

    self_base = os.environ.get("CSG_API_BASE", "http://127.0.0.1:8000").rstrip("/")
    # Research -> Outreach hand-off stays opt-in: only point it at this
    # server if Research's own .env (or the real environment) had it
    # configured at all -- an unset OUTREACH_API_BASE must keep meaning
    # "hand-off disabled", exactly as it does standalone.
    if os.environ.get("OUTREACH_API_BASE") or research_env.get("OUTREACH_API_BASE"):
        os.environ["OUTREACH_API_BASE"] = f"{self_base}/outreach"
    # Outreach -> Research is required for everything (not opt-in), same as
    # standalone, just pointed at this server's own prefix instead of :8000.
    os.environ["RESEARCH_API_BASE"] = f"{self_base}/research"


def _set_default(key: str, value: str | None) -> None:
    if value and key not in os.environ:
        os.environ[key] = value


_configure_env()

# Imported only after _configure_env() has set the scoped/prefixed vars --
# each module's own load_dotenv() call happens at this import.
from csg.api import app as research_app  # noqa: E402
from outreach.api import app as outreach_app  # noqa: E402
from outreach.calendly.routes import calendly_webhook  # noqa: E402


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Runs both mounted apps' own lifespans. Required: each one's lifespan
    is what captures its `_MAIN_LOOP` (used by every background job and
    WebSocket progress publish) -- a Mount does NOT run a sub-app's lifespan
    on its own, only the top-level app's is invoked by the server."""
    missing = [
        name for name in (
            "RESEARCH_SUPABASE_URL", "RESEARCH_SUPABASE_SERVICE_KEY",
            "OUTREACH_SUPABASE_URL", "OUTREACH_SUPABASE_SERVICE_KEY",
        )
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            "Server: missing required env var(s): " + ", ".join(missing) +
            " -- check Research_Agent/.env and Outreach_Agent/.env (see .env.example in each)."
        )

    loop = __import__("asyncio").get_running_loop()
    # Two sub-apps now share one default executor for their sync DB-bound
    # route handlers and background jobs (each previously had its own, one
    # per process) -- doubled so total available worker-thread capacity is
    # unchanged from running them as two separate processes.
    default_workers = min(32, (os.cpu_count() or 1) + 4)
    loop.set_default_executor(ThreadPoolExecutor(max_workers=default_workers * 2))

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(research_app.router.lifespan_context(research_app))
        await stack.enter_async_context(outreach_app.router.lifespan_context(outreach_app))
        yield


app = FastAPI(title="CSG Agents API", lifespan=_lifespan)

app.mount("/research", research_app)
app.mount("/outreach", outreach_app)

# Root alias for the Calendly webhook -- delegates to the exact same handler
# outreach.calendly.routes mounts at /outreach/calendly/webhook, so an
# already-registered Calendly subscription pointed at the pre-existing
# /calendly/webhook path keeps working without re-registration. No logic
# duplicated: this is the same function object, just also reachable at the
# un-prefixed path.
app.add_api_route("/calendly/webhook", calendly_webhook, methods=["POST"])
