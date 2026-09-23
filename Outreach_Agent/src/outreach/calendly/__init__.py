"""Calendly integration: real API sync + webhook receiver for the Bookings
page, layered on top of the pre-existing classifier-only booking detection
(conversation/classifier.py's Calendly-notification-email heuristic, which is
unchanged and still runs).

Self-contained, same convention as conversation/, learning/, optimization/:

    client.py   -- CalendlyClient, a thin httpx wrapper over api.calendly.com
    webhook.py  -- signature verification + payload -> normalized event
    sync.py     -- pull scheduled events, match to prospects/items, write
                   calendly_events, advance booked items
    db.py       -- calendly_events repository functions
    routes.py   -- APIRouter: POST /calendly/webhook, POST /calendly/sync

Mounted in api.py exactly like the other three packages. Everything here is
additive: it never changes how the classifier-only path reaches `booked`, it
only gives the Bookings page a second, independent source of truth backed by
Calendly's own API instead of email heuristics alone.
"""
