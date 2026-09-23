"""Thin HTTP client for Calendly's REST API (api.calendly.com).

Same shape as research_client.py's ResearchClient: a persisted httpx.Client,
a _get/_post helper that raises a custom error on network failure or a 4xx/5xx
status, no retries, explicit timeout. Every call needs CALENDLY_API_TOKEN
(Config.calendly_api_token) -- constructing a client with no token is allowed
(callers check `client.configured` first) so the rest of the app can degrade
gracefully instead of raising at import time.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from ..config import Config

API_BASE = "https://api.calendly.com"


class CalendlyClientError(RuntimeError):
    pass


class CalendlyClient:
    def __init__(self, cfg: Config) -> None:
        self.token = cfg.calendly_api_token
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
            timeout=30.0,
        )
        self._current_user_uri: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CalendlyClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _call(self, method: str, path: str, fn: Any, **kwargs: Any) -> Any:
        if not self.configured:
            raise CalendlyClientError(
                "CALENDLY_API_TOKEN is not set -- see .env.example"
            )
        try:
            r = fn(path, **kwargs)
        except httpx.RequestError as exc:
            raise CalendlyClientError(f"Calendly API unreachable at {API_BASE}{path}: {exc}") from exc
        if r.status_code >= 400:
            raise CalendlyClientError(f"{method} {API_BASE}{path} -> {r.status_code}: {r.text}")
        return r.json() if r.content else None

    def _get(self, path: str, **params: Any) -> Any:
        return self._call(
            "GET", path, self._client.get,
            params={k: v for k, v in params.items() if v is not None},
        )

    def _post(self, path: str, json: dict[str, Any]) -> Any:
        return self._call("POST", path, self._client.post, json=json)

    def _delete(self, path: str) -> Any:
        return self._call("DELETE", path, self._client.delete)

    def current_user(self) -> dict[str, Any]:
        """GET /users/me -- the account owner's resource URI, needed as the
        `user`/`organization` filter on every other list call. Cached for the
        lifetime of this client instance."""
        data = self._get("/users/me")
        return data["resource"]

    def current_user_uri(self) -> str:
        if self._current_user_uri is None:
            self._current_user_uri = self.current_user()["uri"]
        return self._current_user_uri

    def list_scheduled_events(
        self, *, min_start_time: Optional[str] = None, max_start_time: Optional[str] = None,
        status: Optional[str] = None, page_token: Optional[str] = None, count: int = 100,
    ) -> dict[str, Any]:
        """One page of GET /scheduled_events for the account owner (`user`
        filter -- this integration is single-user/account, matching the
        existing single-account-wide calendly_url). Returns the raw envelope
        (`collection` + `pagination.next_page_token`) so the caller
        (sync.fetch_all_scheduled_events) owns the pagination loop rather
        than this method silently fetching everything -- a huge account
        history should never surprise-block on one call."""
        return self._get(
            "/scheduled_events",
            user=self.current_user_uri(),
            min_start_time=min_start_time,
            max_start_time=max_start_time,
            status=status,
            count=count,
            page_token=page_token,
        )

    def get_scheduled_event(self, event_uuid_or_uri: str) -> dict[str, Any]:
        path = event_uuid_or_uri if event_uuid_or_uri.startswith("/") else f"/scheduled_events/{event_uuid_or_uri}"
        return self._get(path)["resource"]

    def list_event_invitees(self, event_uri: str, *, page_token: Optional[str] = None) -> dict[str, Any]:
        """GET /scheduled_events/{uuid}/invitees -- invitee email/name/status
        (active|canceled) live here, NOT on the event resource itself."""
        event_uuid = event_uri.rstrip("/").rsplit("/", 1)[-1]
        return self._get(f"/scheduled_events/{event_uuid}/invitees", page_token=page_token, count=100)

    def create_webhook_subscription(self, callback_url: str, events: list[str], *, scope: str = "user") -> dict[str, Any]:
        """POST /webhook_subscriptions. Returns the created resource,
        including `signing_key` -- Calendly hands this back exactly once, so
        the caller (cli.py's `calendly register-webhook` command) is
        responsible for surfacing it for the operator to save as
        CALENDLY_WEBHOOK_SIGNING_KEY; this client does not persist it."""
        body: dict[str, Any] = {
            "url": callback_url,
            "events": events,
            "organization": self.current_user()["current_organization"],
            "scope": scope,
        }
        if scope == "user":
            body["user"] = self.current_user_uri()
        return self._post("/webhook_subscriptions", json=body)["resource"]

    def cancel_event(self, event_uuid_or_uri: str, *, reason: Optional[str] = None) -> dict[str, Any]:
        """POST /scheduled_events/{uuid}/cancellation -- cancels the event for
        Calendly's own event, not just this integration's local view of it.
        A real cancellation, not a delete: Calendly keeps the event (marked
        canceled) and, if a webhook subscription exists, later delivers its
        own invitee.canceled webhook for it too -- apply_event's upsert-by-
        event_uri makes that redelivery a no-op against the row this call
        already updates via sync.cancel_item_booking."""
        event_uuid = event_uuid_or_uri.rstrip("/").rsplit("/", 1)[-1]
        body: dict[str, Any] = {"reason": reason} if reason else {}
        return self._post(f"/scheduled_events/{event_uuid}/cancellation", json=body)["resource"]

    def mark_no_show(self, invitee_uri: str) -> dict[str, Any]:
        """POST /invitee_no_shows -- flags one invitee as a no-show. Distinct
        from cancellation: the event stays 'active', this is purely an
        annotation Calendly stores against the invitee for reporting."""
        return self._post("/invitee_no_shows", json={"invitee": invitee_uri})["resource"]

    def unmark_no_show(self, no_show_uuid_or_uri: str) -> None:
        """DELETE /invitee_no_shows/{uuid} -- undo a mark_no_show call."""
        no_show_uuid = no_show_uuid_or_uri.rstrip("/").rsplit("/", 1)[-1]
        self._delete(f"/invitee_no_shows/{no_show_uuid}")

    def list_event_types(self, *, user_uri: Optional[str] = None, page_token: Optional[str] = None, count: int = 100) -> dict[str, Any]:
        """One page of GET /event_types for the account owner (defaults to
        current_user_uri(), same single-account assumption as
        list_scheduled_events). Returns the raw envelope so the caller owns
        pagination, same convention as every other list_* method here."""
        return self._get(
            "/event_types",
            user=user_uri or self.current_user_uri(),
            page_token=page_token,
            count=count,
        )

    def list_availability_schedules(self, *, user_uri: Optional[str] = None) -> dict[str, Any]:
        """GET /user_availability_schedules -- an account's availability
        schedule(s). Read-only: Calendly's API has no endpoint to edit
        availability, only its own web UI does."""
        return self._get("/user_availability_schedules", user=user_uri or self.current_user_uri())

    def create_scheduling_link(self, event_type_uri: str) -> dict[str, Any]:
        """POST /scheduling_links -- a real, working mutation (unlike
        activate/deactivate on an event type or editing availability, which
        Calendly's API doesn't support). max_event_count=1 makes this a
        single-use link, the "one-off shareable link" the Bookings/Event
        Types page offers per event type."""
        body = {"max_event_count": 1, "owner": event_type_uri, "owner_type": "EventType"}
        return self._post("/scheduling_links", json=body)["resource"]

