"""Analytics dashboard backend: read-only aggregation over the existing
Outreach tables, by sending account, campaign, follow-up and booking.

Self-contained, like learning/ and optimization/: api.py's single
`include_router(analytics_router)` is its only hook. The pre-existing
`/analytics` endpoint is unchanged.
"""

from .routes import router

__all__ = ["router"]
